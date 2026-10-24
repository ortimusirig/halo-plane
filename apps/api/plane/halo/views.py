# Copyright (c) 2026 Applexus Technologies. Halo changes to Plane Community Edition.
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""API v1 page routes: Plane's own page views (app/views/page), signed in with a personal access token.

Permissions are Plane's ProjectPagePermission and the rules inside PageViewSet (private pages for their owner,
project membership, the guest rule, archive by owner or admin), with the guest rule also applied to versions.
Name and body changes go through edit.edit_page. HaloPagesDescriptionViewSet replaces Plane's description
endpoint for the web app and live, so a save from an editor that missed an API edit merges with it.
"""

import base64
import json

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.db.models import Q
from rest_framework import status
from rest_framework.response import Response

from plane.api.middleware.api_authentication import APIKeyAuthentication
from plane.api.rate_limit import ApiKeyRateThrottle
from plane.app.permissions import ROLE
from plane.app.serializers import PageBinaryUpdateSerializer, PageDetailSerializer, PageSerializer
from plane.app.views.page.base import PagesDescriptionViewSet, PageViewSet
from plane.app.views.page.version import PageVersionEndpoint
from plane.bgtasks.page_transaction_task import page_transaction
from plane.bgtasks.page_version_task import track_page_version
from plane.db.models import Page, PageVersion, Project, ProjectMember
from plane.utils.error_codes import ERROR_CODES
from plane.utils.exception_logger import log_exception

from . import lease, live, ydoc
from .edit import EditRefused, edit_page, sanitize, text_in_sync

EDIT_FIELDS = {"name", "description_html", "append_html", "restore_version_id", "expected_updated_at", "allow_removal"}
# Plane's PageSerializer would also take parent (any page id), is_locked and archived_at; the API does not.
CREATE_FIELDS = {"name", "description_html", "access"}


def page_url(slug, project_id, page_id) -> str:
    return f"{(settings.WEB_URL or '').rstrip('/')}/{slug}/projects/{project_id}/pages/{page_id}"


def version_url(slug, project_id, page_id, version_id):
    return f"{page_url(slug, project_id, page_id)}?paneTab=info&version={version_id}" if version_id else None


def _error(message, code, status_code=status.HTTP_400_BAD_REQUEST):
    return Response({"error": message, "code": code}, status=status_code)


def _guest_limited(request, slug, project_id) -> bool:
    """Plane's rule (PageViewSet.list and retrieve): a guest without guest_view_all_features sees own pages only."""
    return (
        ProjectMember.objects.filter(
            workspace__slug=slug, project_id=project_id, member=request.user, role=ROLE.GUEST.value, is_active=True
        ).exists()
        and not Project.objects.get(pk=project_id).guest_view_all_features
    )


class PersonalTokenMixin:
    authentication_classes = [APIKeyAuthentication]

    def get_throttles(self):
        return [ApiKeyRateThrottle()]


class HaloProjectPageViewSet(PersonalTokenMixin, PageViewSet):
    def list(self, request, slug, project_id):
        queryset = self.get_queryset()
        if _guest_limited(request, slug, project_id):
            queryset = queryset.filter(owned_by=request.user)

        def results(pages):
            data = PageSerializer(pages, many=True).data
            for item in data:
                item["url"] = page_url(slug, project_id, item["id"])
            return data

        return self.paginate(request=request, queryset=queryset, on_results=results)

    def retrieve(self, request, slug, project_id, page_id=None):
        response = super().retrieve(request, slug, project_id, page_id)
        if response.status_code == status.HTTP_200_OK:
            page = Page.objects.get(pk=page_id)
            response.data["work_item_ids"] = [str(item) for item in response.data.pop("issue_ids", [])]
            response.data.update(
                {
                    "description_text": page.description_stripped or "",
                    "url": page_url(slug, project_id, page_id),
                    # False while the stored HTML lags an editor's merged save (API edits wait for it).
                    "text_complete": text_in_sync(page.description_html, page.description_binary),
                }
            )
        return response

    def create(self, request, slug, project_id):
        unknown = sorted(set(request.data) - CREATE_FIELDS)
        if unknown:
            return _error(
                "Halo's API creates a page from name, description_html and access only, not: " + ", ".join(unknown),
                "unsupported_field",
            )
        try:
            html = sanitize(request.data.get("description_html") or "<p></p>") or "<p></p>"
        except EditRefused as refused:
            return Response(refused.payload(), status=refused.status)
        # Like Plane's duplicate endpoint: no document yet; live builds it from the HTML on first open.
        serializer = PageSerializer(
            data={key: request.data[key] for key in ("name", "access") if key in request.data},
            context={
                "project_id": project_id,
                "owned_by_id": request.user.id,
                "description_json": {},
                "description_binary": None,
                "description_html": html,
            },
        )
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        serializer.save()
        page_transaction.delay(new_description_html=html, old_description_html=None, page_id=serializer.data["id"])
        data = PageDetailSerializer(self.get_queryset().get(pk=serializer.data["id"])).data
        data["url"] = page_url(slug, project_id, data["id"])
        return Response(data, status=status.HTTP_201_CREATED)

    def partial_update(self, request, slug, project_id, page_id):
        unknown = sorted(set(request.data) - EDIT_FIELDS)
        if unknown:
            return _error(
                "Halo's API changes only name and text (description_html or append_html), not: " + ", ".join(unknown),
                "unsupported_field",
            )
        replace, appended, name, restore_id = (
            request.data.get(key) for key in ("description_html", "append_html", "name", "restore_version_id")
        )
        if sum(value is not None for value in (replace, appended, restore_id)) > 1:
            return _error("Give one of description_html (replace), append_html (add at the end) and "
                          "restore_version_id (undo), not several.", "invalid_field")
        if replace is None and appended is None and restore_id is None and name is None:
            return _error("Give name, description_html, append_html or restore_version_id.", "nothing_to_change")
        if not all(value is None or isinstance(value, str) for value in (replace, appended, name, restore_id)):
            return _error("name, description_html, append_html and restore_version_id must be text.", "invalid_field")
        page = Page.objects.get(
            pk=page_id, workspace__slug=slug, projects__id=project_id, project_pages__deleted_at__isnull=True
        )
        if page.parent_id:
            # Plane's page views leave sub-pages out (PageViewSet.get_queryset); so does this API.
            return _error("This page is a sub-page; edit it in Halo.", "page_hierarchy")
        restore_from = None
        if restore_id is not None:
            restore_from = PageVersion.objects.filter(pk=restore_id, page_id=page.id).first()
            if restore_from is None:
                return _error("No such version of this page.", "unknown_version", status.HTTP_404_NOT_FOUND)
        try:
            result = edit_page(
                page,
                request.user,
                html=replace if appended is None else appended,
                append=appended is not None,
                name=name,
                expected_updated_at=request.data.get("expected_updated_at"),
                allow_removal=request.data.get("allow_removal") is True,
                restore_from=restore_from,
            )
        except EditRefused as refused:
            return Response(refused.payload(), status=refused.status)
        except live.LiveUnavailable:
            return _error(
                "Halo's editor service is not reachable, so the page was not changed. Try again in a minute.",
                "live_unavailable",
                status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        data = PageDetailSerializer(self.get_queryset().get(pk=page_id)).data
        data.update(result)
        data["url"] = page_url(slug, project_id, page_id)
        data["before_version_url"] = version_url(slug, project_id, page_id, result["before_version_id"])
        data["after_version_url"] = version_url(slug, project_id, page_id, result["after_version_id"])
        return Response(data, status=status.HTTP_200_OK)

    def _in_a_hierarchy(self, page_id):
        """Plane archives and unarchives a page with all its sub-pages and may detach it from its parent."""
        page = Page.objects.filter(pk=page_id).only("parent_id").first()
        if page and (page.parent_id or Page.objects.filter(parent_id=page_id).exists()):
            return _error("This page has sub-pages or is one; archive or unarchive it in Halo, where the effect "
                          "on the other pages is shown.", "page_hierarchy")
        return None

    def archive(self, request, slug, project_id, page_id):
        if refusal := self._in_a_hierarchy(page_id):
            return refusal
        # Archived pages refuse saves, so an open editor's unsaved typing could never be stored.
        try:
            if live.is_open(page_id):
                return _error("Someone has this page open in Halo. Ask them to close it, then try again.",
                              "page_open", status.HTTP_409_CONFLICT)
        except live.LiveUnavailable:
            return _error("Halo's editor service is not reachable, so the page was not archived.",
                          "live_unavailable", status.HTTP_503_SERVICE_UNAVAILABLE)
        return super().archive(request, slug, project_id, page_id)

    def unarchive(self, request, slug, project_id, page_id):
        if refusal := self._in_a_hierarchy(page_id):
            return refusal
        return super().unarchive(request, slug, project_id, page_id)


class HaloPageVersionEndpoint(PersonalTokenMixin, PageVersionEndpoint):
    def get(self, request, slug, project_id, page_id, pk=None):
        page = Page.objects.filter(pk=page_id).only("owned_by_id").first()
        if page and page.owned_by_id != request.user.id and _guest_limited(request, slug, project_id):
            return _error("You are not allowed to view this page.", "forbidden", status.HTTP_403_FORBIDDEN)
        response = super().get(request, slug, project_id, page_id, pk)
        if pk and response.status_code == status.HTTP_200_OK:
            version = PageVersion.objects.filter(pk=pk).only("description_stripped").first()
            response.data["description_text"] = (version.description_stripped if version else "") or ""
        return response


class HaloPagesDescriptionViewSet(PagesDescriptionViewSet):
    """Plane's description endpoint: the live editor loads and saves pages through it, signed in as the person.

    Loading waits while an API edit of the page runs, and fails rather than load a page an edit is still
    writing. A page that has no document yet gets one here, built from its HTML by live's converter, so the
    first editor never converts on its own while an API edit writes the same page. Saving is Plane's
    PagesDescriptionViewSet.partial_update (same checks, serializer and tasks) under a row lock, with two
    changes: a save that lacks part of the stored document (an API edit that editor never received) is merged
    with it instead of overwriting it, and the background tasks start only after the save is committed.
    """

    def _visible_page(self, request, slug, project_id, page_id):
        # Plane's lookup in PagesDescriptionViewSet: the person's own pages and public pages of the project.
        return Page.objects.filter(
            Q(owned_by=request.user) | Q(access=0),
            pk=page_id,
            workspace__slug=slug,
            projects__id=project_id,
            project_pages__deleted_at__isnull=True,
        ).first()

    def retrieve(self, request, slug, project_id, page_id):
        if not lease.wait_until_free(page_id):
            return _error("An edit of this page is still running; reload the page in a moment.",
                          "edit_in_progress", status.HTTP_503_SERVICE_UNAVAILABLE)
        page = self._visible_page(request, slug, project_id, page_id)
        if page is not None and not page.description_binary:
            try:
                self._build_first_document(page.pk)
            except live.LiveUnavailable as error:
                # Too large for live's converter (or not reachable): live then converts the page itself, as
                # in stock Plane. An API edit cannot race it: an edit of this page needs the same converter.
                log_exception(error)
        return super().retrieve(request, slug, project_id, page_id)

    @staticmethod
    def _build_first_document(page_pk):
        with transaction.atomic():
            page = Page.objects.select_for_update().get(pk=page_pk)
            if page.description_binary:
                return
            binary, json_data = live.convert_html(page.description_html or "<p></p>")
            # update(), not save(): building the document is not a change, so updated_at stays.
            Page.objects.filter(pk=page_pk).update(description_binary=binary, description_json=json_data)

    def partial_update(self, request, slug, project_id, page_id):
        found = self._visible_page(request, slug, project_id, page_id)
        if found is None:
            raise Page.DoesNotExist
        missing = None
        with transaction.atomic():
            page = Page.objects.select_for_update().get(pk=found.pk)
            if page.is_locked:
                return Response({"error_code": ERROR_CODES["PAGE_LOCKED"], "error_message": "PAGE_LOCKED"},
                                status=status.HTTP_400_BAD_REQUEST)
            if page.archived_at:
                return Response({"error_code": ERROR_CODES["PAGE_ARCHIVED"], "error_message": "PAGE_ARCHIVED"},
                                status=status.HTTP_400_BAD_REQUEST)
            data = dict(request.data.items())
            encoded = data.get("description_binary")
            merged = None
            if encoded and page.description_binary:
                try:
                    merged = ydoc.missing_from(base64.b64decode(encoded), bytes(page.description_binary))
                except Exception as error:  # not a document Halo can read: Plane's serializer answers it
                    log_exception(error)
            if merged:
                union, missing = merged
                data["description_binary"] = base64.b64encode(union).decode()
            old_description_html = page.description_html
            existing_instance = json.dumps({"description_html": old_description_html}, cls=DjangoJSONEncoder)
            serializer = PageBinaryUpdateSerializer(page, data=data, partial=True)
            if not serializer.is_valid():
                return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
            serializer.save()
            if data.get("description_html"):
                new_html = data["description_html"]
                transaction.on_commit(lambda: page_transaction.delay(
                    new_description_html=new_html, old_description_html=old_description_html, page_id=page_id))
            if not merged:
                # A merged save's HTML is that editor's older text, so it makes no version; the editor's next
                # save, complete because it received the missing part, does.
                user_id = request.user.id
                transaction.on_commit(lambda: track_page_version.delay(
                    page_id=page_id, existing_instance=existing_instance, user_id=user_id))
        if missing:
            try:
                live.publish_update(page_id, missing)
            except Exception as error:  # the merged document is saved; the editor gets it on its next load
                log_exception(error)
        return Response({"message": "Updated successfully"})
