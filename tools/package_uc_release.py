"""Build a deterministic, secret-free paired UC interface release package.

This is a packaging helper only. It never connects to a database, SSH host, Agent,
or Docker daemon. It copies selected source files, writes deployment notes, and
records SHA-256 hashes for the two archives.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
import tarfile
from io import BytesIO
from pathlib import Path

BACKEND_DEFAULT = Path(__file__).resolve().parents[1]
FRONTEND_DEFAULT = BACKEND_DEFAULT.parent / "TS-frontend"
RELEASE_DATE = "20260930"

BACKEND_FILES = [
    ".env.example", "Dockerfile", "README.md", "requirements.txt",
    "app", "tools/simulate_uc_requests.py", "tools/request_examples",
]
FRONTEND_FILES = [
    ".env.example", "Dockerfile", "README.md", "next.config.ts", "package.json",
    "pnpm-lock.yaml", "pnpm-workspace.yaml", "postcss.config.mjs", "eslint.config.mjs",
    "tsconfig.json", "public", "src",
]

BACKEND_DEPLOYMENT = """# TS Backend UC interfaces release — {date} {label}

This package is a source release for the UC candidate and UC diagram bridge. It
contains no `.env`, credentials, cookies, database dumps, SQL migration bundles,
virtual environments, tests, logs, or Docker image. Deployment is manual and was
not executed by the packaging helper.

## Contract

- `POST /v1/agent/generations`: browser-authenticated request; the backend reads the complete accepted PRD and calls Agent with `FROM_PRD_CREATE_UC`.
- `GET /v1/agent/uc-template?scene=FROM_PRD_CREATE_UC`: Agent-only callback protected by `X-Agent-Internal-Key`.
- `POST /v1/agent/uc-diagram`: browser-authenticated request; the backend reads accepted UC revisions, short-circuits on `known_uc_source_version`, and calls Agent with `FROM_UC_CREATE_UC_DIAGRAM` only when the source changed.

The browser never sends PRD正文 to Agent and never receives an Agent key. This
release does not add a database migration or formal UC save/confirm behavior.

## Server configuration

Copy the existing reviewed server configuration; do not replace it with the
placeholders below. Verify `AGENT_INVOKE_URL`, `AGENT_API_KEY`,
`AGENT_CALLBACK_KEY`, project read/write DSNs, CORS, and timeout values. The
callback key is separate from the Agent API key.

Cookie mode must match the external browser URL:

- temporary direct HTTP validation: `COOKIE_SECURE=false`;
- production HTTPS (including TLS termination at a reviewed reverse proxy):
  `COOKIE_SECURE=true`.

Do not enable or disable business writes, rerun migrations, reset accounts, or
change the existing database DSNs as part of this UC bridge release.

## Manual Docker steps

Run each block in a subshell so `set -euo pipefail` does not alter the caller's
interactive shell. First inspect the existing `ts-agent-backend` container and
port 7000. Do not start a second container on the same port and do not stop the
existing service until a planned cutover has been approved.

```bash
(
  set -euo pipefail
  umask 077
  RELEASE_DIR=/data/ts-platform-managed/{date}/backend/{name}
  IMAGE=ts-agent-backend:{date}-{label}
  cd "$RELEASE_DIR"
  test -f .env
  docker build --pull=false -t "$IMAGE" .
  # Use only during the approved cutover; port 7000 must be free.
  docker run --name {name} --network host --restart unless-stopped \\
    --env-file "$RELEASE_DIR/.env" "$IMAGE" \\
    python -m uvicorn app.main:app --host 192.168.64.3 --port 7000
)
```

The command is intentionally not an automatic stop/remove/rollback script.
After a planned cutover, check `/health`, `/docs`, login/me, the template
callback authentication, and the two browser-authenticated UC endpoints. Use
`tools/simulate_uc_requests.py` only with real, authorized test IDs.
"""

FRONTEND_DEPLOYMENT = """# TS Frontend UC interfaces release — {date} {label}

This package is paired with `ts-backend-{date}-{label}`. It contains the UC
candidate display and the database-mode UC diagram request/status/code-preview
flow. Mermaid code is shown as inert text; Mermaid rendering remains deferred by
product scope. Deployment is manual and was not executed by the packaging
helper.

## Build-time/runtime proxy target

`BACKEND_URL` must point to the paired business backend, not Agent. The Dockerfile
keeps this value available both at build time and when `next start` evaluates
`next.config.ts` again. `NEXT_PUBLIC_API_BASE_URL=/api` and
`NEXT_PUBLIC_USE_MOCKS=false` are embedded during build.

## Manual Docker steps

First inspect port 7100, any existing frontend container, and the reverse proxy.
Do not stop unrelated containers and do not bind a second process to 7100.

```bash
(
  set -euo pipefail
  umask 077
  RELEASE_DIR=/data/ts-platform-managed/{date}/frontend/{name}
  IMAGE=ts-agent-frontend:{date}-{label}
  cd "$RELEASE_DIR"
  docker build --pull=false --build-arg BACKEND_URL=http://192.168.64.3:7000 -t "$IMAGE" .
  # Use only during the approved cutover; port 7100 must be free.
  docker run --name {name} --network host --restart unless-stopped \\
    -e BACKEND_URL=http://192.168.64.3:7000 "$IMAGE"
)
```

The frontend listens on `192.168.64.3:7100`. Keep `/api` same-origin through
the reviewed reverse proxy so the HttpOnly session cookie is sent to the
business backend. Do not put Agent credentials in any frontend variable.

Post-cutover, verify the login page, `/api/health`, login/me/logout, project
workspace, UC candidate request, and UC diagram refresh. `empty` and `unchanged`
are valid diagram responses; they are not Agent/rendering failures.
"""


def copy_selected(root: Path, destination: Path, entries: list[str]) -> None:
    for relative in entries:
        source = root / relative
        if not source.exists():
            raise FileNotFoundError(source)
        target = destination / relative
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", ".next", "node_modules", ".env", ".env.*"))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def write_file(destination: Path, relative: str, content: str) -> None:
    path = destination / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def add_tree(tar: tarfile.TarFile, root: Path, arc_root: str) -> None:
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        arcname = f"{arc_root}/{relative}"
        if path.is_dir():
            info = tarfile.TarInfo(arcname + "/")
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.mtime = 0
            tar.addfile(info)
            continue
        if any(part in {"__pycache__", "node_modules", ".next"} for part in path.parts):
            continue
        data = path.read_bytes()
        info = tarfile.TarInfo(arcname)
        info.size = len(data)
        info.mode = 0o755 if path.name == "simulate_uc_requests.py" else 0o644
        info.mtime = 0
        tar.addfile(info, BytesIO(data))


def archive(source: Path, archive_path: Path, arc_root: str) -> tuple[str, int]:
    raw = BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        add_tree(tar, source, arc_root)
    compressed = gzip.compress(raw.getvalue(), compresslevel=9, mtime=0)
    archive_path.write_bytes(compressed)
    return hashlib.sha256(compressed).hexdigest(), len(compressed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-root", type=Path, default=BACKEND_DEFAULT)
    parser.add_argument("--frontend-root", type=Path, default=FRONTEND_DEFAULT)
    parser.add_argument("--output-root", type=Path, default=FRONTEND_DEFAULT / "tmp" / "uc-release-20260930")
    parser.add_argument("--release-date", default=RELEASE_DATE, help="release folder date, YYYYMMDD")
    parser.add_argument("--label", default="uc-r2")
    args = parser.parse_args()
    if not args.release_date.isdigit() or len(args.release_date) != 8:
        parser.error("--release-date must be YYYYMMDD")

    date = args.release_date
    backend_name = f"ts-backend-{date}-{args.label}"
    frontend_name = f"ts-frontend-{date}-{args.label}"
    output = args.output_root.resolve()
    if output.exists():
        shutil.rmtree(output)
    backend_stage = output / backend_name
    frontend_stage = output / frontend_name
    backend_stage.mkdir(parents=True)
    frontend_stage.mkdir(parents=True)
    copy_selected(args.backend_root.resolve(), backend_stage, BACKEND_FILES)
    copy_selected(args.frontend_root.resolve(), frontend_stage, FRONTEND_FILES)
    write_file(backend_stage, "DEPLOYMENT.md", BACKEND_DEPLOYMENT.format(date=date, label=args.label, name=backend_name))
    write_file(frontend_stage, "DEPLOYMENT.md", FRONTEND_DEPLOYMENT.format(date=date, label=args.label, name=frontend_name))

    backend_archive = output / f"{backend_name}.tar.gz"
    frontend_archive = output / f"{frontend_name}.tar.gz"
    backend_hash, backend_bytes = archive(backend_stage, backend_archive, backend_name)
    frontend_hash, frontend_bytes = archive(frontend_stage, frontend_archive, frontend_name)
    manifest = {
        "release_date": f"{date[:4]}-{date[4:6]}-{date[6:]}",
        "scope": "UC generation and UC diagram backend/frontend paired source release",
        "company_deployment_executed": False,
        "agent_source_modified": False,
        "database_migration_executed": False,
        "docker_build_executed": False,
        "artifacts": [
            {"name": backend_name, "archive": str(backend_archive), "sha256": backend_hash, "bytes": backend_bytes},
            {"name": frontend_name, "archive": str(frontend_archive), "sha256": frontend_hash, "bytes": frontend_bytes},
        ],
    }
    (output / "release-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "SHA256SUMS").write_text(f"{backend_hash}  {backend_archive.name}\n{frontend_hash}  {frontend_archive.name}\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())