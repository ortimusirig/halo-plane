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
from django.contrib.postgres.aggregates import ArrayAgg
from django.contrib.postgres.fields import ArrayField
from django.db.models import Q, UUIDField, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from plane.api.middleware.api_authentication import APIKeyAuthentication
from plane.api.rate_limit import ApiKeyRateThrottle
from plane.app.permissions import ROLE
from plane.app.serializers import PageBinaryUpdateSerializer, PageDetailSerializer, PageSerializer
from plane.app.views.page.base import PageDuplicateEndpoint, PagesDescriptionViewSet, PageViewSet
from plane.app.views.page.version import PageVersionEndpoint
from plane.bgtasks.page_transaction_task import page_transaction
from plane.db.models import Page, PageVersion, Project, ProjectMember, ProjectPage
from plane.utils.error_codes import ERROR_CODES
from plane.utils.exception_logger import log_exception

from . import live, ydoc
from .edit import EditRefused, edit_page, first_document, sanitize, text_behind
from .models import PageBeingCopied, PageTextBehind
from .tasks import add_to_index, finish_page_copy, index_page, index_restored_page, track_page_version_after_save

EDIT_FIELDS = {"name", "description_html", "append_html", "restore_version_id", "expected_updated_at", "allow_removal"}
# Plane's PageSerializer would also take parent (any page id), is_locked and archived_at; the API does not.
CREATE_FIELDS = {"name", "description_html", "access"}


def page_url(slug, project_id, page_id) -> str:
    return f"{(settings.WEB_URL or '').rstrip('/')}/{slug}/projects/{project_id}/pages/{page_id}"


def version_url(slug, project_id, page_id, version_id):
    return f"{page_url(slug, project_id, page_id)}?paneTab=info&version={version_id}" if version_id else None


def _error(message, code, status_code=status.HTTP_400_BAD_REQUEST):
    return Response({"error": message, "code": code}, status=status_code)


# Live's converter takes up to 100 kB of HTML (express.json's default limit in apps/live).
CONVERTER_LIMIT_BYTES = 100_000
EDITABLE_HINTS = {
    "not_opened_yet": "Open the page once in Halo; then its text can be edited here.",
    "live_unavailable": "Open the page once in Halo; then its text can be edited here.",
    "page_too_large_for_editor": "The page is too large for Halo's editor service; edit its text in Halo.",
}


def _editability(has_document: bool, reason: str | None = None) -> dict:
    """Whether Halo's API can edit the page's text (it needs the page's editor document) and, if not, why and
    what to do: said in every read and create, never found out from a refused edit."""
    if has_document:
        return {"text_editable": True}
    return {"text_editable": False, "text_editable_reason": reason, "text_editable_hint": EDITABLE_HINTS[reason]}


def _queue(task, **kwargs) -> bool:
    """Queue a background task after the change is committed. A broker failure is logged, not answered as an
    error: the change is saved, and a client that retried would make it twice."""
    try:
        task.delay(**kwargs)
        return True
    except Exception as error:
        log_exception(error)
        return False


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
    def filter_queryset(self, queryset):
        # ?search and the other list filters narrow the list only: a page read or changed by its id is found whatever
        # its name, so a rename under ?search=<old name> answers with the renamed page (code review of 4231b9c).
        if self.action == "list":
            return super().filter_queryset(queryset)
        return queryset

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
            # A mention whose target is not a work item id is indexed without one (tasks._uuid_or_none): not listed.
            response.data["work_item_ids"] = [str(item) for item in response.data.pop("issue_ids", []) if item]
            response.data.update(
                {
                    "description_text": page.description_stripped or "",
                    "url": page_url(slug, project_id, page_id),
                    # False while the stored HTML lags an editor's merged save (API edits wait for it).
                    "text_complete": not text_behind(page_id),
                    **_editability(
                        bool(page.description_binary),
                        "page_too_large_for_editor"
                        if len((page.description_html or "").encode()) > CONVERTER_LIMIT_BYTES
                        else "not_opened_yet",
                    ),
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
        if not isinstance(request.data.get("description_html") or "", str):
            return _error("description_html must be text.", "invalid_field")
        if not Project.objects.filter(pk=project_id, workspace__slug=slug, archived_at__isnull=True).exists():
            # Plane's page list and reads leave archived projects out (PageViewSet.get_queryset); creating a page
            # there would commit a page the answer could not show.
            return _error("No such project, or it is archived.", "project_not_found", status.HTTP_404_NOT_FOUND)
        try:
            html = sanitize(request.data.get("description_html") or "<p></p>") or "<p></p>"
        except EditRefused as refused:
            return Response(refused.payload(), status=refused.status)
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
        # The first document now, while nobody can be loading the page (it does not exist yet), with its name as
        # title. Without live (or for HTML too large for it) the page is made without one, as Plane's create
        # makes it, and the answer says so.
        reason = None
        try:
            binary, json_data = first_document(html, serializer.validated_data.get("name") or "")
            serializer.context.update(description_binary=binary, description_json=json_data)
        except live.LiveTooLarge:
            reason = "page_too_large_for_editor"
        except live.LiveUnavailable:
            reason = "live_unavailable"
        with transaction.atomic():
            # The page and its project link (PageSerializer.create) commit together, and only with the answer.
            serializer.save()
            data = PageDetailSerializer(self.get_queryset().get(pk=serializer.data["id"])).data
        # The page-scoped job that reads the page's text when it runs, as for the API's edits: a create job finishing
        # after a later edit's job cannot index what that edit removed (code review of 4231b9c).
        _queue(index_restored_page, page_id=data["id"], after_edit=True)
        data["url"] = page_url(slug, project_id, data["id"])
        data.update(_editability(reason is None, reason))
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
        if not self.get_queryset().filter(pk=page_id).exists():
            # Plane's own view of the person's pages (archived projects and others' private pages left out):
            # checked before anything changes, so the answer after the edit can never be a 404.
            return _error("No such page in this project.", "page_not_found", status.HTTP_404_NOT_FOUND)
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


class HaloAppPageViewSet(PageViewSet):
    """Plane's own page views for the web app, with one change: the page's row is locked before Plane reads it.

    Plane's lock, access and properties updates read the whole page and save every field. Without the lock, one
    that read the page while an API edit was running would write the old text back after the edit committed.
    """

    @staticmethod
    def _lock_row(page_id):
        Page.objects.select_for_update().filter(pk=page_id).first()

    def partial_update(self, request, slug, project_id, page_id):
        """Plane's PageViewSet.partial_update (the same checks, serializer and answers; fingerprinted by the
        upgrade gate) under the page's row lock, with its page_transaction task queued after commit, so a broker
        failure cannot undo the save. It writes description_html without the document, as in Plane (the web app's
        store sends its whole copy of the page); when that changes the HTML the page is marked, so Halo's API
        reports text_complete false and waits for the next complete editor save."""
        with transaction.atomic():
            try:
                page = Page.objects.select_for_update(of=("self",)).get(
                    pk=page_id, workspace__slug=slug, projects__id=project_id, project_pages__deleted_at__isnull=True
                )
                if page.is_locked:
                    return Response({"error": "Page is locked"}, status=status.HTTP_400_BAD_REQUEST)
                parent = request.data.get("parent", None)
                if parent:
                    _ = Page.objects.get(
                        pk=parent, workspace__slug=slug, projects__id=project_id, project_pages__deleted_at__isnull=True
                    )
                # Only update access if the page owner is the requesting  user
                if page.access != request.data.get("access", page.access) and page.owned_by_id != request.user.id:
                    return Response(
                        {"error": "Access cannot be updated since this page is owned by someone else"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                serializer = PageDetailSerializer(page, data=request.data, partial=True)
                page_description = page.description_html
                merged_marked = PageTextBehind.objects.filter(page_id=page.pk, merged=True).exists()
                if serializer.is_valid():
                    serializer.save()
                    if page.description_html != page_description:
                        PageTextBehind.objects.update_or_create(page_id=page.pk, defaults={"since": timezone.now()})
                    if request.data.get("description_html"):
                        new_html = request.data.get("description_html", "<p></p>")
                        if merged_marked:
                            # The document keeps parts the stored HTML may lack: add-only (tasks.add_to_index).
                            transaction.on_commit(lambda: _queue(add_to_index, page_id=page_id, html=new_html))
                        else:
                            transaction.on_commit(lambda: _queue(
                                page_transaction, new_description_html=new_html,
                                old_description_html=page_description, page_id=page_id))
                    return Response(serializer.data, status=status.HTTP_200_OK)
                return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
            except Page.DoesNotExist:
                return Response(
                    {"error": "Access cannot be updated since this page is owned by someone else"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

    def lock(self, request, slug, project_id, page_id):
        with transaction.atomic():
            self._lock_row(page_id)
            return super().lock(request, slug, project_id, page_id)

    def unlock(self, request, slug, project_id, page_id):
        with transaction.atomic():
            self._lock_row(page_id)
            return super().unlock(request, slug, project_id, page_id)

    def access(self, request, slug, project_id, page_id):
        with transaction.atomic():
            self._lock_row(page_id)
            return super().access(request, slug, project_id, page_id)


class HaloPageDuplicateEndpoint(PageDuplicateEndpoint):
    """Plane's duplicate (PageDuplicateEndpoint.post, the same steps and answers), with three changes. The source
    page is locked while it is copied. The copy takes the source's document (the authoritative part, with the
    copy's name as title) instead of none, so nothing is converted in the request; a source without a document
    gives a copy without one, as in Plane, and a source whose HTML lags its document passes that mark on. The
    copy task is Halo's finish_page_copy, which changes only the copy's text (Plane's task saves the whole page
    it read, which could undo an API edit); until it has run, API edits of the copy are refused."""

    def post(self, request, slug, project_id, page_id):
        with transaction.atomic():
            page = Page.objects.select_for_update(of=("self",)).get(
                pk=page_id, workspace__slug=slug, projects__id=project_id, project_pages__deleted_at__isnull=True
            )
            if page.access == Page.PRIVATE_ACCESS and page.owned_by_id != request.user.id:
                return Response({"error": "Permission denied"}, status=status.HTTP_403_FORBIDDEN)
            project_ids = list(ProjectPage.objects.filter(page_id=page_id).values_list("project_id", flat=True))
            document = bytes(page.description_binary) if page.description_binary else None
            # The source's mark passes on as it is (merged too: the copy's document keeps what its HTML lacks).
            mark = PageTextBehind.objects.filter(page_id=page_id).first() if document else None
            page.pk = None
            page.name = f"{page.name} (Copy)"
            # The document's own title is the source's; the copy's name goes there (or is added), the body stays.
            page.description_binary = (
                ydoc.with_title(ydoc.edit_document(document, name=page.name).state, page.name) if document else None
            )
            page.owned_by = request.user
            page.created_by = request.user
            page.updated_by = request.user
            page.save()
            for project_id in project_ids:
                ProjectPage.objects.create(
                    workspace_id=page.workspace_id,
                    project_id=project_id,
                    page_id=page.id,
                    created_by_id=page.created_by_id,
                    updated_by_id=page.updated_by_id,
                )
            PageBeingCopied.objects.create(page_id=page.id, since=timezone.now())
            if mark is not None:
                PageTextBehind.objects.create(page_id=page.id, since=timezone.now(), merged=mark.merged)
        _queue(page_transaction, new_description_html=page.description_html, old_description_html=None, page_id=page.id)
        # Plane passes the last project of the loop to its copy task; so does Halo.
        if not _queue(finish_page_copy, page_id=page.id, project_id=project_id, user_id=request.user.id):
            # No copy task will run: the copy is usable at once and keeps showing the original's images.
            PageBeingCopied.objects.filter(page_id=page.id).delete()
        page = (
            Page.objects.filter(pk=page.id)
            .annotate(
                project_ids=Coalesce(
                    ArrayAgg("projects__id", distinct=True, filter=~Q(projects__id=True)),
                    Value([], output_field=ArrayField(UUIDField())),
                )
            )
            .first()
        )
        return Response(PageDetailSerializer(page).data, status=status.HTTP_201_CREATED)


class HaloPagesDescriptionViewSet(PagesDescriptionViewSet):
    """Plane's description endpoint: the live editor loads and saves pages through it, signed in as the person.

    Loading is Plane's own retrieve, not overridden (the upgrade gate fails if this class defines anything but
    partial_update and _visible_page). Saving is Plane's PagesDescriptionViewSet.partial_update (same checks,
    serializer and tasks) under a row lock, with two changes: a save that lacks part of the stored document (an
    API edit that editor never received) and shares its history is merged with it instead of overwriting it, and
    the background tasks start only after the save is committed.
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
            # What was written, as Plane's serializer saw it (an empty value is written too).
            html_saved = "description_html" in serializer.validated_data
            document_saved = "description_binary" in serializer.validated_data
            merged_marked = PageTextBehind.objects.filter(page_id=page.pk, merged=True).exists()
            cleared_merged = False
            if merged:
                # The stored HTML is now that editor's older text: marked (merged) until a complete save.
                PageTextBehind.objects.update_or_create(page_id=page.pk,
                                                        defaults={"since": timezone.now(), "merged": True})
            elif html_saved != document_saved:
                # A save of only one of HTML and document: marked until a complete save (an earlier merged mark
                # stays as it is).
                PageTextBehind.objects.get_or_create(page_id=page.pk, defaults={"since": timezone.now()})
            elif document_saved:
                mark = PageTextBehind.objects.filter(page_id=page.pk).first()
                if mark is not None:
                    cleared_merged = mark.merged
                    mark.delete()
            if cleared_merged:
                # The complete save that ends the merged-marked state: Halo's job brings the index in line with
                # the stored text (the removals the add-only saves held back).
                transaction.on_commit(lambda: _queue(index_page, page_id=page_id))
            elif data.get("description_html"):
                new_html = data["description_html"]
                if merged or merged_marked:
                    # The document keeps parts this HTML lacks, and Plane's delete of an entry is permanent (a soft
                    # delete, unique per page): add-only, through Plane's own task (tasks.add_to_index).
                    transaction.on_commit(lambda: _queue(add_to_index, page_id=page_id, html=new_html))
                else:
                    # Plane's own indexing, with Plane's arguments.
                    transaction.on_commit(lambda: _queue(
                        page_transaction, new_description_html=new_html, old_description_html=old_description_html,
                        page_id=page_id))
            if not merged:
                # A merged save's HTML is that editor's older text, so it makes no version; the editor's next
                # save, complete because it received the missing part, does.
                user_id = request.user.id
                transaction.on_commit(lambda: _queue(
                    track_page_version_after_save, page_id=page_id, existing_instance=existing_instance,
                    user_id=user_id))
        if missing:
            try:
                live.publish_update(page_id, missing)
            except Exception as error:  # the merged document is saved; the editor gets it on its next load
                log_exception(error)
        return Response({"message": "Updated successfully"})
