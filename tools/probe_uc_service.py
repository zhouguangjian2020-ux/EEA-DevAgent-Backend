"""Portable UC handoff probes. No repository imports, secret files or deployment changes."""
from __future__ import annotations

import argparse
import getpass
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from uuid import uuid4
import warnings

import httpx

SCENES = {"FROM_PRD_CREATE_UC", "FROM_UC_CREATE_UC_DIAGRAM"}
UC_FIELDS = {
    "name", "description", "number", "actors", "preconditions", "scenario",
    "postconditions", "extensions", "hmi", "regulations", "performance", "nonfunctional",
}


class ProbeFailure(Exception):
    pass


def hidden_input(label: str) -> str:
    # Fail closed if getpass would fall back to an echoing/non-terminal input.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        return getpass.getpass(label)


def validate_url(value: str, *, local_only: bool = False) -> str:
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError
        _ = parsed.port
        if local_only and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError
    except ValueError:
        raise ProbeFailure("INVALID_LOCAL_AGENT_URL" if local_only else "INVALID_BASE_URL") from None
    return value.rstrip("/")


def safe_token(value: object) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        return value
    return None


def checked_json(response: httpx.Response, label: str) -> dict:
    print(f"{label}_HTTP_STATUS={response.status_code}")
    try:
        data = response.json()
    except (ValueError, UnicodeError):
        data = None
    if response.status_code != 200:
        detail = data.get("detail", data.get("error", {})) if isinstance(data, dict) else {}
        if isinstance(detail, dict):
            code = safe_token(detail.get("code"))
            request_id = safe_token(detail.get("request_id") or data.get("trace_id"))
            if code:
                print(f"ERROR_CODE={code}")
            if request_id:
                print(f"REQUEST_ID={request_id}")
        # Never echo raw response text, headers, cookies or server error messages.
        raise ProbeFailure(f"{label}_FAILED")
    if not isinstance(data, dict):
        raise ProbeFailure(f"{label}_INVALID_JSON_OBJECT")
    return data


def execute(mode: str, *, base_url: str = "http://192.168.64.3:7000",
            origin: str = "http://192.168.64.3:7100", username: str = "engineer01",
            show_template: bool = False, agent_url: str | None = None,
            request_file: str | None = None, prompt=hidden_input,
            transport: httpx.BaseTransport | None = None) -> None:
    if mode not in {"health", "session", "template", "agent-local"}:
        raise ProbeFailure("UNKNOWN_MODE")
    base = validate_url(base_url)
    headers = {"Accept": "application/json"}
    if mode != "agent-local":
        headers["Origin"] = origin
    timeout = 330 if mode == "agent-local" else 15
    with httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False,
                      headers=headers, transport=transport) as client:
        if mode == "health":
            data = checked_json(client.get(base + "/health"), "HEALTH")
            if data.get("status") != "ok":
                raise ProbeFailure("HEALTH_INVALID_STATUS")
            print("HEALTH_OK")
        elif mode == "session":
            password = prompt("Login password (input hidden): ")
            if not password:
                raise ProbeFailure("EMPTY_LOGIN_PASSWORD")
            checked_json(client.post(base + "/v1/auth/login",
                         json={"username": username, "password": password}), "LOGIN")
            data = checked_json(client.get(base + "/v1/auth/me"), "SESSION")
            if not isinstance(data.get("username"), str) or not data["username"]:
                raise ProbeFailure("SESSION_INVALID_RESPONSE")
            print("SESSION_OK")
        elif mode == "template":
            key = prompt("AGENT_CALLBACK_KEY (input hidden): ")
            if not key or any(c.isspace() for c in key):
                raise ProbeFailure("INVALID_CALLBACK_KEY_INPUT")
            data = checked_json(client.get(base + "/v1/agent/uc-template",
                params={"scene": "FROM_PRD_CREATE_UC"},
                headers={"X-Agent-Internal-Key": key}), "UC_TEMPLATE")
            fields = data.get("fields")
            schema = data.get("candidate_schema")
            if (data.get("scene") != "FROM_PRD_CREATE_UC"
                or not isinstance(fields, list) or len(fields) != 12
                or any(not isinstance(item, dict) for item in fields)
                or {item.get("name") for item in fields} != UC_FIELDS
                or not isinstance(schema, dict)
                or set(schema.get("properties", {})) != UC_FIELDS - {"number"}):
                raise ProbeFailure("UC_TEMPLATE_CONTRACT_MISMATCH")
            print("UC_TEMPLATE_OK formal_fields=12 candidate_fields=11")
            if show_template:
                print("UC_TEMPLATE_RESPONSE")
                print(json.dumps({k: data[k] for k in
                    ("scene", "template_id", "template_version", "fields", "candidate_schema", "instructions")
                    if k in data}, ensure_ascii=False, indent=2))
        else:
            if not agent_url or not request_file:
                raise ProbeFailure("LOCAL_AGENT_URL_AND_REQUEST_FILE_REQUIRED")
            url = validate_url(agent_url, local_only=True)
            body = json.loads(Path(request_file).read_text(encoding="utf-8-sig"))
            if (not isinstance(body, dict) or body.get("scene") not in SCENES
                    or body.get("response_format") != "json"):
                raise ProbeFailure("INVALID_UC_AGENT_EXAMPLE")
            # Each manual run is new; do not reuse fixture idempotency/trace identifiers.
            body["idempotency_key"] = str(uuid4())
            body["trace_id"] = "uc-handoff-" + uuid4().hex
            key = prompt("Local Agent API key (input hidden): ")
            if not key or any(c.isspace() for c in key):
                raise ProbeFailure("INVALID_AGENT_KEY_INPUT")
            print("LOCAL_AGENT_SCENE=" + body["scene"])
            print("REQUEST_TRACE_ID=" + body["trace_id"])
            data = checked_json(client.post(url, json=body,
                                headers={"X-API-Key": key}), "LOCAL_AGENT")
            print("LOCAL_AGENT_RESPONSE")
            print(json.dumps(data, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("health", "session", "template", "agent-local"))
    parser.add_argument("--base-url", default="http://192.168.64.3:7000")
    parser.add_argument("--origin", default="http://192.168.64.3:7100")
    parser.add_argument("--username", default="engineer01")
    parser.add_argument("--show-template", action="store_true")
    parser.add_argument("--agent-url")
    parser.add_argument("--request-file")
    args = parser.parse_args()
    try:
        execute(**vars(args))
    except ProbeFailure as error:
        print(str(error))
        return 1
    except httpx.RequestError as error:
        print("NETWORK_ERROR=" + type(error).__name__)
        return 1
    except (OSError, ValueError, getpass.GetPassWarning, EOFError) as error:
        print("LOCAL_INPUT_ERROR=" + type(error).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
