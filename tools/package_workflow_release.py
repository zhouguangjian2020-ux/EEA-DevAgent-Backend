"""Create source-only workflow release archives; no Docker/SSH/DB operations.

Allowlist production inputs, reject links/unsafe archive paths, preserve the
reviewed SQL bundle byte-for-byte, and refuse to overwrite any output folder.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
import tarfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {'.git', '.venv', 'node_modules', '.next', '__pycache__', '.pytest_cache', 'tmp', 'artifacts', 'tests', 'docs', 'data'}
DOCKERIGNORE = '''.git
.env
.env.*
!.env.example
.venv
__pycache__
.pytest_cache
node_modules
.next
tmp
artifacts
tests
docs
data
*.pyc
'''


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def collect(root: Path, entries: list[str]) -> dict[str, bytes]:
    result = {}
    for entry in entries:
        source = root / entry
        if not source.exists() or source.is_symlink():
            raise ValueError(f'Missing or linked source: {source}')
        candidates = sorted(source.rglob('*')) if source.is_dir() else [source]
        for path in candidates:
            rel = path.relative_to(root)
            if path.is_symlink():
                raise ValueError(f'Linked source: {path}')
            if any(part in EXCLUDED for part in rel.parts):
                continue
            if not path.is_file() or path.suffix in {'.pyc', '.pyo'}:
                continue
            if path.name == '.env' or path.name.startswith('.env.') and path.name != '.env.example':
                continue
            key = rel.as_posix()
            if key in result:
                raise ValueError(f'Duplicate source: {key}')
            result[key] = path.read_bytes()
    return result


def safe_name(name: str) -> bool:
    p = PurePosixPath(name)
    return bool(name) and not p.is_absolute() and '..' not in p.parts and '\\' not in name and ':' not in name and not any(ord(c) < 32 for c in name)


def make_checks(files: dict[str, bytes]) -> bytes:
    return (''.join(sha(data) + '  ' + name + '\n' for name, data in sorted(files.items()))).encode('utf-8')


def verify_checks(files: dict[str, bytes]) -> None:
    raw = files['SHA256SUMS']
    if b'\r' in raw or raw.startswith(b'\xef\xbb\xbf') or not raw.endswith(b'\n'):
        raise ValueError('Checksum manifest must use LF, no BOM')
    seen = set()
    for line in raw.decode('utf-8').splitlines():
        digest, name = line.split('  ', 1)
        if name in seen or not safe_name(name) or not re.fullmatch('[a-f0-9]{64}', digest):
            raise ValueError('Invalid or duplicate checksum entry')
        if name not in files or sha(files[name]) != digest:
            raise ValueError(f'Checksum mismatch: {name}')
        seen.add(name)
    if seen != set(files) - {'SHA256SUMS'}:
        raise ValueError('Checksum coverage mismatch')


def tar_bytes(name: str, source: dict[str, bytes], *, preserve_manifest: bool = False) -> bytes:
    if not re.fullmatch('[a-z0-9][a-z0-9.-]*', name) or any(not safe_name(p) for p in source):
        raise ValueError('Unsafe archive name/path')
    files = dict(source)
    if preserve_manifest:
        verify_checks(files)
    else:
        if 'SHA256SUMS' in files:
            raise ValueError('Refusing duplicate archive checksum manifest')
        files['SHA256SUMS'] = make_checks(files)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w', format=tarfile.USTAR_FORMAT) as archive:
        for rel, data in sorted(files.items()):
            entry = tarfile.TarInfo(name + '/' + rel)
            entry.size, entry.mode, entry.mtime = len(data), 0o644, 0
            archive.addfile(entry, io.BytesIO(data))
    return gzip.compress(buffer.getvalue(), compresslevel=9, mtime=0)


def inspect_archive(data: bytes, root: str) -> dict[str, bytes]:
    files = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
        for entry in archive.getmembers():
            if not entry.isfile() or not entry.name.startswith(root + '/'):
                raise ValueError('Unexpected archive member')
            rel = entry.name[len(root) + 1:]
            if not safe_name(rel) or rel in files:
                raise ValueError('Unsafe/duplicate archive member')
            files[rel] = archive.extractfile(entry).read()
    verify_checks(files)
    return files


def prepare_sources(front: Path, back: Path, console: Path, tag: str):
    backend_files = collect(back, ['app', 'requirements.txt', '.env.example', 'tools/simulate_uc_requests.py', 'tools/probe_uc_service.py'])
    # Source is the reviewed console deployment recipe, NOT TS-backend/deployment.
    preview_recipe = (console / 'deployment/Backend.Dockerfile').read_bytes()
    if b'FROM python:3.12-slim' not in preview_recipe or b'"7018"' not in preview_recipe:
        raise ValueError('Unexpected reviewed preview backend Dockerfile')
    main_recipe = preview_recipe.replace(b'EXPOSE 7018', b'EXPOSE 7000').replace(b'"127.0.0.1", "--port", "7018"', b'"192.168.64.3", "--port", "7000"')
    if b'EXPOSE 7000' not in main_recipe or b'"7000"' not in main_recipe:
        raise ValueError('Main backend port transformation failed')
    common = {**backend_files, '.dockerignore': DOCKERIGNORE.encode()}
    main = {**common, 'Dockerfile': main_recipe}
    preview = {**common, 'Dockerfile': preview_recipe}
    frontend = collect(front, ['src', 'public', '.env.example', 'Dockerfile', 'next.config.ts', 'package.json', 'pnpm-lock.yaml', 'pnpm-workspace.yaml', 'postcss.config.mjs', 'eslint.config.mjs', 'tsconfig.json'])
    frontend['.dockerignore'] = DOCKERIGNORE.encode()
    console_files = collect(console, ['labconsole', 'requirements.txt', 'Dockerfile', '.env.example'])
    console_files['.dockerignore'] = DOCKERIGNORE.encode()
    sql_root = front / 'docs/current/database/agent-workflows-r1'
    sql_files = {p.name: p.read_bytes() for p in sorted(sql_root.iterdir()) if p.is_file() and not p.is_symlink()}
    verify_checks(sql_files)
    return [
        ('ts-backend-' + tag, main, False),
        ('ts-frontend-' + tag, frontend, False),
        ('ts-agent-console-' + tag, console_files, False),
        ('ts-backend-agent-console-' + tag, preview, False),
        ('ts-db-agent-workflows-' + tag, sql_files, True),
    ]


def package(front: Path, back: Path, console: Path, output: Path, tag: str, notes: Path) -> dict:
    if not re.fullmatch(r'20[0-9]{6}-workflows-r[1-9][0-9]*', tag):
        raise ValueError('Use YYYYMMDD-workflows-rN tag')
    if output.exists():
        raise ValueError('Refusing to overwrite any existing release folder')
    # Read and validate ALL inputs before creating output, including SQL hashes.
    sources = prepare_sources(front, back, console, tag)
    blobs, records = {}, []
    for name, files, preserve in sources:
        blob = tar_bytes(name, files, preserve_manifest=preserve)
        restored = inspect_archive(blob, name)
        assert all(restored[k] == v for k, v in files.items())
        blobs[name + '.tar.gz'] = blob
        records.append({'name': name + '.tar.gz', 'root': name, 'sha256': sha(blob), 'bytes': len(blob), 'files': len(restored)})
    manifest = {
        'release_tag': tag, 'scope': 'FR generation, allocation lifecycle, private project chat and console',
        'company_deployment_executed': False, 'database_migration_executed': False,
        'company_docker_build_executed': False, 'agent_source_modified': False,
        'runtime_archives_exclude_docs_tests_private_env': True,
        'backend_python_runtime': '3.12', 'archives': records,
        'approved_target': json.loads((console / 'deployment/integration-target.json').read_text(encoding='utf-8')),
        'dependency_fingerprints': {
            'backend_requirements': sha((back / 'requirements.txt').read_bytes()),
            'console_requirements': sha((console / 'requirements.txt').read_bytes()),
            'frontend_package': sha((front / 'package.json').read_bytes()),
            'frontend_lock': sha((front / 'pnpm-lock.yaml').read_bytes()),
        },
        'deployment_order': ['upload_and_verify', 'build_without_cutover', 'database_readonly_inspection_and_backup', 'compatible_backend_cutover', 'reviewed_sql_apply_verify', 'frontend_cutover', 'separately_review_console_cutover_and_write_policy'],
        'preview_business_writes': 'preserve_false; explicit approval required for durable chat tests',
    }
    blobs['manifest.json'] = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    blobs['RELEASE.md'] = notes.read_text(encoding='utf-8').replace('\r\n', '\n').encode('utf-8')
    blobs['SHA256SUMS'] = make_checks(blobs)
    verify_checks(blobs)
    output.mkdir(parents=True, exist_ok=False)
    for name, data in blobs.items():
        with (output / name).open('xb') as f:
            f.write(data)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frontend', type=Path, default=ROOT.parent / 'TS-frontend')
    parser.add_argument('--backend', type=Path, default=ROOT)
    parser.add_argument('--console', type=Path, default=ROOT.parent / 'TS-agent-console')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--notes', type=Path, required=True)
    a = parser.parse_args()
    print(json.dumps(package(a.frontend, a.backend, a.console, a.output, a.tag, a.notes), ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
