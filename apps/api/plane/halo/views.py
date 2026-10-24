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

from django.conf import settings
from django.db import transaction
from rest_framework import status
from rest_framework.response import Response

from plane.api.middleware.api_authentication import APIKeyAuthentication
from plane.api.rate_limit import ApiKeyRateThrottle
from plane.app.permissions import ROLE
from plane.app.serializers import PageDetailSerializer, PageSerializer
from plane.app.views.page.base import PagesDescriptionViewSet, PageViewSet
from plane.app.views.page.version import PageVersionEndpoint
from plane.bgtasks.page_transaction_task import page_transaction
from plane.db.models import Page, PageVersion, Project, ProjectMember
from plane.utils.exception_logger import log_exception

from . import lease, live, ydoc
from .edit import EditRefused, edit_page, sanitize

EDIT_FIELDS = {"name", "description_html", "append_html", "expected_updated_at", "allow_removal"}
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
                {"description_text": page.description_stripped or "", "url": page_url(slug, project_id, page_id)}
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
        replace, appended, name = (request.data.get(key) for key in ("description_html", "append_html", "name"))
        if replace is not None and appended is not None:
            return _error("Give description_html (replace) or append_html (add at the end), not both.", "invalid_field")
        if replace is None and appended is None and name is None:
            return _error("Give name, description_html or append_html.", "nothing_to_change")
        if not all(value is None or isinstance(value, str) for value in (replace, appended, name)):
            return _error("name, description_html and append_html must be text.", "invalid_field")
        page = Page.objects.get(
            pk=page_id, workspace__slug=slug, projects__id=project_id, project_pages__deleted_at__isnull=True
        )
        try:
            result = edit_page(
                page,
                request.user,
                html=replace if appended is None else appended,
                append=appended is not None,
                name=name,
                expected_updated_at=request.data.get("expected_updated_at"),
                allow_removal=request.data.get("allow_removal") is True,
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

    def archive(self, request, slug, project_id, page_id):
        # Archived pages refuse saves, so an open editor's unsaved typing could never be stored.
        try:
            if live.is_open(page_id):
                return _error("Someone has this page open in Halo. Ask them to close it, then try again.",
                              "page_open", status.HTTP_409_CONFLICT)
        except live.LiveUnavailable:
            return _error("Halo's editor service is not reachable, so the page was not archived.",
                          "live_unavailable", status.HTTP_503_SERVICE_UNAVAILABLE)
        return super().archive(request, slug, project_id, page_id)


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
    """Plane's description endpoint (the live editor loads and saves pages through it, signed in as the person).

    Loading waits while an API edit of the page is running. A save that lacks part of the stored document (an
    API edit that editor never received) is merged with it, so nothing is lost, and the missing part is sent to
    that editor through live. Every other request is Plane's own code.
    """

    def retrieve(self, request, slug, project_id, page_id):
        lease.wait_until_free(page_id)
        return super().retrieve(request, slug, project_id, page_id)

    def partial_update(self, request, slug, project_id, page_id):
        encoded = request.data.get("description_binary")
        if not encoded:
            return super().partial_update(request, slug, project_id, page_id)
        missing = None
        with transaction.atomic():
            stored = Page.objects.select_for_update().filter(pk=page_id).values_list("description_binary", flat=True)
            stored = stored.first()
            if stored:
                try:
                    merged = ydoc.missing_from(base64.b64decode(encoded), bytes(stored))
                except Exception as error:  # not a document Halo can read: Plane's own checks answer it
                    log_exception(error)
                    merged = None
                if merged:
                    union, missing = merged
                    data = dict(request.data.items())
                    data["description_binary"] = base64.b64encode(union).decode()
                    request._full_data = data
            response = super().partial_update(request, slug, project_id, page_id)
        if missing and response.status_code == status.HTTP_200_OK:
            try:
                live.publish_update(page_id, missing)
            except Exception as error:  # the merged document is saved; the editor gets it on its next load
                log_exception(error)
        return response
