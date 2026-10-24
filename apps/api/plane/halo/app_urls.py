# Copyright (c) 2026 Applexus Technologies. Halo changes to Plane Community Edition.
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Halo's replacements for Plane's web-app page routes that write a page (the web app and live use them);
included before plane.app.urls, so they answer first. Same paths and methods as plane/app/urls/page.py."""

from django.urls import path

from .views import HaloAppPageViewSet, HaloPagesDescriptionViewSet

PAGE = "workspaces/<str:slug>/projects/<uuid:project_id>/pages/<uuid:page_id>/"

urlpatterns = [
    path(
        PAGE,
        HaloAppPageViewSet.as_view({"get": "retrieve", "patch": "partial_update", "delete": "destroy"}),
        name="halo-app-page",
    ),
    path(f"{PAGE}lock/", HaloAppPageViewSet.as_view({"post": "lock", "delete": "unlock"}), name="halo-app-page-lock"),
    path(f"{PAGE}access/", HaloAppPageViewSet.as_view({"post": "access"}), name="halo-app-page-access"),
    path(
        f"{PAGE}description/",
        HaloPagesDescriptionViewSet.as_view({"get": "retrieve", "patch": "partial_update"}),
        name="halo-page-description",
    ),
]
