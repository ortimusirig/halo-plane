# Copyright (c) 2026 Applexus Technologies. Halo changes to Plane Community Edition.
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""API v1 page routes: Plane's own page views (app/views/page), signed in with a personal access token.

Permissions are Plane's ProjectPagePermission and the rules inside PageViewSet (private pages for their owner,
project membership, the guest rule, archive by owner or admin). Name and body changes go through edit.edit_page.
"""

from django.conf import settings
from rest_framework import status
from rest_framework.response import Response

from plane.api.middleware.api_authentication import APIKeyAuthentication
from plane.api.rate_limit import ApiKeyRateThrottle
from plane.app.permissions import ROLE
from plane.app.serializers import PageDetailSerializer, PageSerializer
from plane.app.views.page.base import PageViewSet
from plane.app.views.page.version import PageVersionEndpoint
from plane.bgtasks.page_transaction_task import page_transaction
from plane.db.models import Page, Project, ProjectMember

from . import live
from .edit import EditRefused, edit_page, sanitize

EDIT_FIELDS = {"name", "description_html", "expected_updated_at"}


def page_url(slug, project_id, page_id) -> str:
    return f"{(settings.WEB_URL or '').rstrip('/')}/{slug}/projects/{project_id}/pages/{page_id}"


def version_url(slug, project_id, page_id, version_id):
    return f"{page_url(slug, project_id, page_id)}?paneTab=info&version={version_id}" if version_id else None


def _error(message, code, status_code=status.HTTP_400_BAD_REQUEST):
    return Response({"error": message, "code": code}, status=status_code)


class PersonalTokenMixin:
    authentication_classes = [APIKeyAuthentication]

    def get_throttles(self):
        return [ApiKeyRateThrottle()]


class HaloProjectPageViewSet(PersonalTokenMixin, PageViewSet):
    def list(self, request, slug, project_id):
        queryset = self.get_queryset()
        project = Project.objects.get(pk=project_id)
        # Plane's rule (PageViewSet.list): guests without guest_view_all_features see only their own pages.
        if (
            ProjectMember.objects.filter(
                workspace__slug=slug, project_id=project_id, member=request.user, role=ROLE.GUEST.value, is_active=True
            ).exists()
            and not project.guest_view_all_features
        ):
            queryset = queryset.filter(owned_by=request.user)
        return self.paginate(
            request=request, queryset=queryset, on_results=lambda pages: PageSerializer(pages, many=True).data
        )

    def retrieve(self, request, slug, project_id, page_id=None):
        response = super().retrieve(request, slug, project_id, page_id)
        if response.status_code == status.HTTP_200_OK:
            page = Page.objects.get(pk=page_id)
            try:
                is_open = live.is_open(page_id)
            except live.LiveUnavailable:
                is_open = None
            response.data["work_item_ids"] = [str(item) for item in response.data.pop("issue_ids", [])]
            response.data.update(
                {
                    "description_text": page.description_stripped or "",
                    "url": page_url(slug, project_id, page_id),
                    "is_open": is_open,
                }
            )
        return response

    def create(self, request, slug, project_id):
        try:
            html = sanitize(request.data.get("description_html") or "<p></p>") or "<p></p>"
        except EditRefused as refused:
            return Response(refused.payload(), status=refused.status)
        # Like Plane's duplicate endpoint: no document yet; live builds it from the HTML on first open.
        serializer = PageSerializer(
            data=request.data,
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
            return _error("Halo's API changes only name and description_html, not: " + ", ".join(unknown),
                          "unsupported_field")
        html, name = request.data.get("description_html"), request.data.get("name")
        if html is None and name is None:
            return _error("Give name, description_html or both.", "nothing_to_change")
        if not all(value is None or isinstance(value, str) for value in (html, name)):
            return _error("name and description_html must be text.", "invalid_field")
        page = Page.objects.get(
            pk=page_id, workspace__slug=slug, projects__id=project_id, project_pages__deleted_at__isnull=True
        )
        try:
            result = edit_page(
                page, request.user, html=html, name=name, expected_updated_at=request.data.get("expected_updated_at")
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


class HaloPageVersionEndpoint(PersonalTokenMixin, PageVersionEndpoint):
    pass
