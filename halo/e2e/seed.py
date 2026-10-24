# Copyright (c) 2026 Applexus Technologies. Halo changes to Plane Community Edition.
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Test data for halo/e2e (run with manage.py shell): a person, workspace, project, token and session."""

import json

from django.conf import settings

from plane.db.models import APIToken, Project, ProjectMember, User, Workspace, WorkspaceMember
from plane.db.models.session import SessionStore

user, _ = User.objects.get_or_create(email="e2e@applexus.com", defaults={"username": "e2e", "first_name": "E2E"})
workspace, _ = Workspace.objects.get_or_create(slug="e2e", defaults={"name": "E2E", "owner": user})
WorkspaceMember.objects.get_or_create(workspace=workspace, member=user, defaults={"role": 20})
project, _ = Project.objects.get_or_create(workspace=workspace, identifier="E2E", defaults={"name": "E2E pages"})
ProjectMember.objects.get_or_create(project=project, member=user, defaults={"role": 20, "workspace": workspace})
token = APIToken.objects.create(user=user, label="e2e", workspace=workspace)

session = SessionStore()
session["_auth_user_id"] = str(user.id)
session["_auth_user_backend"] = settings.AUTHENTICATION_BACKENDS[0]
session["_auth_user_hash"] = user.get_session_auth_hash()
session.create()

print("SEED=" + json.dumps({
    "user_id": str(user.id),
    "slug": workspace.slug,
    "project_id": str(project.id),
    "token": token.token,
    "cookie": f"{settings.SESSION_COOKIE_NAME}={session.session_key}",
}))
