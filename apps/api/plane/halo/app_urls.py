# Copyright (c) 2026 Applexus Technologies. Halo changes to Plane Community Edition.
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Halo's replacement for Plane's page description route (the web app and live use it); included before
plane.app.urls, so it answers first. Same path as plane/app/urls/page.py "page-description"."""

from django.urls import path

from .views import HaloPagesDescriptionViewSet

urlpatterns = [
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/pages/<uuid:page_id>/description/",
        HaloPagesDescriptionViewSet.as_view({"get": "retrieve", "patch": "partial_update"}),
        name="halo-page-description",
    ),
]
