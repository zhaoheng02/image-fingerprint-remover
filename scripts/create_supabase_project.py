"""Create a Supabase project via the official Management API.

Required environment variables:
  SUPABASE_ACCESS_TOKEN  Personal access token or OAuth access token
  SUPABASE_DB_PASSWORD   Database password for the new project

Optional:
  SUPABASE_ORG_SLUG      Organization slug. If omitted and the account has
                         exactly one organization, that organization is used.

Example:
  SUPABASE_ACCESS_TOKEN=sbp_... \
  SUPABASE_DB_PASSWORD='long-random-password' \
  python scripts/create_supabase_project.py --name imgclean-prod --region ap-southeast-1
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


API_BASE = "https://api.supabase.com/v1"


def _request(method: str, path: str, token: str, body: dict[str, Any] | None = None) -> Any:
    data = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(
        f"{API_BASE}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            payload = res.read()
            return json.loads(payload.decode("utf-8")) if payload else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} failed: HTTP {exc.code}: {detail}") from exc


def _pick_org(token: str, explicit_slug: str | None) -> str:
    if explicit_slug:
        return explicit_slug

    orgs = _request("GET", "/organizations", token)
    if len(orgs) == 1:
        return orgs[0]["slug"]

    print("Multiple Supabase organizations found. Re-run with SUPABASE_ORG_SLUG set:", file=sys.stderr)
    for org in orgs:
        print(f"  {org.get('slug')}  {org.get('name')}", file=sys.stderr)
    raise SystemExit(2)


def create_project(args: argparse.Namespace) -> dict[str, Any]:
    token = os.environ.get("SUPABASE_ACCESS_TOKEN")
    db_password = os.environ.get("SUPABASE_DB_PASSWORD")
    if not token:
        raise SystemExit("Missing SUPABASE_ACCESS_TOKEN")
    if not db_password:
        raise SystemExit("Missing SUPABASE_DB_PASSWORD")

    org_slug = _pick_org(token, os.environ.get("SUPABASE_ORG_SLUG"))
    body: dict[str, Any] = {
        "name": args.name,
        "organization_slug": org_slug,
        "db_pass": db_password,
        "region_selection": {"type": "specific", "code": args.region},
    }
    if args.instance_size:
        body["desired_instance_size"] = args.instance_size

    project = _request("POST", "/projects", token, body)
    ref = project["ref"]
    if args.wait:
        for _ in range(args.wait_attempts):
            latest = _request("GET", f"/projects/{ref}", token)
            if latest.get("status") == "ACTIVE_HEALTHY":
                project = latest
                break
            time.sleep(args.wait_interval)

    return project


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a Supabase hosted project.")
    parser.add_argument("--name", default="imgclean-prod", help="Project name")
    parser.add_argument("--region", default="ap-southeast-1", help="Supabase region code")
    parser.add_argument("--instance-size", help="Optional instance size, for example nano or micro")
    parser.add_argument("--wait", action="store_true", help="Poll until the project is healthy")
    parser.add_argument("--wait-attempts", type=int, default=60)
    parser.add_argument("--wait-interval", type=int, default=10)
    args = parser.parse_args()

    project = create_project(args)
    public = {
        "ref": project.get("ref"),
        "name": project.get("name"),
        "organization_slug": project.get("organization_slug"),
        "region": project.get("region"),
        "status": project.get("status"),
        "api_url": f"https://{project.get('ref')}.supabase.co" if project.get("ref") else None,
    }
    print(json.dumps(public, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
