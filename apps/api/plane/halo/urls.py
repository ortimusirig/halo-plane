# Copyright (c) 2026 Applexus Technologies. Halo changes to Plane Community Edition.
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Project page routes for API v1, shaped like Plane's documented page API (plane-sdk 0.3.1 paths)."""

from django.urls import path

from .views import HaloPageVersionEndpoint, HaloProjectPageViewSet

PAGES = "workspaces/<str:slug>/projects/<uuid:project_id>/pages/"

urlpatterns = [
    path(PAGES, HaloProjectPageViewSet.as_view({"get": "list", "post": "create"}), name="halo-project-pages"),
    path(
        f"{PAGES}<uuid:page_id>/",
        HaloProjectPageViewSet.as_view({"get": "retrieve", "patch": "partial_update"}),
        name="halo-project-page",
    ),
    path(
        f"{PAGES}<uuid:page_id>/archive/",
        HaloProjectPageViewSet.as_view({"post": "archive", "delete": "unarchive"}),
        name="halo-project-page-archive",
    ),
    path(f"{PAGES}<uuid:page_id>/versions/", HaloPageVersionEndpoint.as_view(), name="halo-project-page-versions"),
    path(
        f"{PAGES}<uuid:page_id>/versions/<uuid:pk>/",
        HaloPageVersionEndpoint.as_view(),
        name="halo-project-page-version",
    ),
]
