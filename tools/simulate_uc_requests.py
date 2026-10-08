"""Simulate browser-origin login and the UC bridge requests without exposing secrets.

The script uses one httpx Client so the HttpOnly ``ts_session`` cookie returned by
/v1/auth/login is automatically reused. It never prints the password, session
cookie, Agent API key, callback key, or a complete database DSN.

Required environment variables:
  UC_USERNAME, UC_PROJECT_ID, UC_FUNCTION_ID
Required when UC_RUN_GENERATION is enabled (the default):
  UC_PRD_ID, UC_PRD_REVISION_ID
Optional:
  UC_API_BASE_URL (default http://127.0.0.1:8000)
  UC_ORIGIN (default http://127.0.0.1:3000; sent as a browser-like Origin header)
  UC_PASSWORD (if omitted, prompt securely without echoing; useful for manual runs)
  UC_KNOWN_UC_SOURCE_VERSION (64 lowercase hex characters, or omitted)
  UC_RUN_GENERATION=0 to skip the UC candidate request
  UC_RUN_DIAGRAM=0 to skip the UC diagram request
"""
from __future__ import annotations

import getpass
import json
import os
import sys
from uuid import UUID, uuid4

import httpx


def env_text(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return default if value is None else value.strip()


def required_env(name: str) -> str:
    value = env_text(name)
    if not value:
        raise SystemExit(f"missing environment variable: {name}")
    return value


def uuid_env(name: str) -> str:
    value = required_env(name)
    try:
        return str(UUID(value))
    except ValueError as error:
        raise SystemExit(f"{name} must be a UUID") from error


def bool_env(name: str, default: bool = True) -> bool:
    value = env_text(name)
    if value is None or value == "":
        return default
    if value.lower() in {"1", "true", "yes", "on"}:
        return True
    if value.lower() in {"0", "false", "no", "off"}:
        return False
    raise SystemExit(f"{name} must be 0/1 or true/false")


def print_json(label: str, value: object) -> None:
    print(label)
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def check(response: httpx.Response, label: str) -> None:
    if response.is_error:
        # Backend errors are deliberately safe; cap the output so an accidental
        # upstream response cannot flood a terminal or disclose unrelated data.
        detail = response.text.replace("\n", " ")
        if len(detail) > 2000:
            detail = detail[:2000] + "…"
        raise SystemExit(f"{label} failed: HTTP {response.status_code} {detail}")


def main() -> int:
    username = required_env("UC_USERNAME")
    password = os.environ.get("UC_PASSWORD")
    if password is None:
        password = getpass.getpass("UC_PASSWORD (input hidden): ")
    if not password:
        raise SystemExit("UC_PASSWORD is empty")

    project_id = uuid_env("UC_PROJECT_ID")
    function_id = uuid_env("UC_FUNCTION_ID")
    run_generation = bool_env("UC_RUN_GENERATION", True)
    run_diagram = bool_env("UC_RUN_DIAGRAM", True)
    if not run_generation and not run_diagram:
        raise SystemExit("enable at least one of UC_RUN_GENERATION or UC_RUN_DIAGRAM")
    prd_id = uuid_env("UC_PRD_ID") if run_generation else None
    prd_revision_id = uuid_env("UC_PRD_REVISION_ID") if run_generation else None
    base_url = env_text("UC_API_BASE_URL", "http://127.0.0.1:8000") or "http://127.0.0.1:8000"
    origin = env_text("UC_ORIGIN", "http://127.0.0.1:3000") or "http://127.0.0.1:3000"
    known_version = env_text("UC_KNOWN_UC_SOURCE_VERSION")
    if known_version is not None and (len(known_version) != 64 or any(c not in "0123456789abcdef" for c in known_version)):
        raise SystemExit("UC_KNOWN_UC_SOURCE_VERSION must be 64 lowercase hexadecimal characters")

    with httpx.Client(
        base_url=base_url.rstrip("/"),
        timeout=330,
        follow_redirects=False,
        headers={"Origin": origin, "Accept": "application/json"},
    ) as client:
        login = client.post("/v1/auth/login", json={"username": username, "password": password})
        check(login, "login")
        print(f"LOGIN_OK user={username}")

        me = client.get("/v1/auth/me")
        check(me, "session check")
        print(f"SESSION_OK username={me.json().get('username', '<unknown>')}")

        if run_generation:
            generation_request = {
                "scene": "FROM_PRD_CREATE_UC",
                "project_id": project_id,
                "function_id": function_id,
                "source": {"kind": "PRD", "artifact_id": prd_id, "revision_id": prd_revision_id},
                "client_request_id": str(uuid4()),
            }
            print_json("UC_GENERATION_REQUEST", generation_request)
            generation = client.post(
                "/v1/agent/generations",
                headers={"X-Project-Request": "1", "Idempotency-Key": generation_request["client_request_id"]},
                json=generation_request,
            )
            check(generation, "UC generation")
            print_json("UC_GENERATION_RESPONSE", generation.json())
        else:
            print("UC_GENERATION_SKIPPED")

        if run_diagram:
            diagram_request = {
                "scene": "FROM_UC_CREATE_UC_DIAGRAM",
                "project_id": project_id,
                "function_id": function_id,
                "known_uc_source_version": known_version,
                "client_request_id": str(uuid4()),
            }
            print_json("UC_DIAGRAM_REQUEST", diagram_request)
            diagram = client.post(
                "/v1/agent/uc-diagram",
                headers={"X-Project-Request": "1", "Idempotency-Key": diagram_request["client_request_id"]},
                json=diagram_request,
            )
            check(diagram, "UC diagram")
            print_json("UC_DIAGRAM_RESPONSE", diagram.json())
        else:
            print("UC_DIAGRAM_SKIPPED")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except httpx.RequestError as error:
        raise SystemExit(f"HTTP request failed: {error.__class__.__name__}") from error