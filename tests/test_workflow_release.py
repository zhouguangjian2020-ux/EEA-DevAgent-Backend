import io
import tarfile

import pytest

from tools.package_workflow_release import collect, inspect_archive, make_checks, package, prepare_sources, sha, tar_bytes, verify_checks


def test_roundtrip_deterministic():
    source = {'app/main.py': b'print("ok")\n', '.env.example': b'KEY=\n'}
    one = tar_bytes('test-r2', source)
    assert one == tar_bytes('test-r2', source)
    restored = inspect_archive(one, 'test-r2')
    assert restored == {**source, 'SHA256SUMS': make_checks(source)}
    assert b'\r' not in restored['SHA256SUMS']


def test_preserved_sql_manifest_has_no_duplicate():
    source = {'apply.sql': b'BEGIN; COMMIT;\n'}
    source['SHA256SUMS'] = make_checks(source)
    blob = tar_bytes('sql-r1', source, preserve_manifest=True)
    assert inspect_archive(blob, 'sql-r1') == source
    with tarfile.open(fileobj=io.BytesIO(blob), mode='r:gz') as archive:
        assert archive.getnames().count('sql-r1/SHA256SUMS') == 1
    with pytest.raises(ValueError, match='duplicate'):
        tar_bytes('sql-r1', source)


@pytest.mark.parametrize('name', ['../x', '/x', 'x/../../p', 'C:/secret', 'x\\p', 'x\ny'])
def test_unsafe_member_rejected(name):
    with pytest.raises(ValueError):
        tar_bytes('safe-r1', {name: b'data'})


@pytest.mark.parametrize('manifest', [b'wrong\n', b'a\r\n', b'\xef\xbb\xbfno\n', b''])
def test_bad_manifests_rejected(manifest):
    with pytest.raises(ValueError):
        verify_checks({'SHA256SUMS': manifest})


def test_collect_excludes_secrets_and_caches(tmp_path):
    for name in ['app/main.py', 'app/.env', 'app/.env.prod', 'app/__pycache__/x.pyc', 'app/tests/x.py']:
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b'data')
    assert collect(tmp_path, ['app']) == {'app/main.py': b'data'}
    with pytest.raises(ValueError):
        collect(tmp_path, ['app', 'app/main.py'])


def test_nonempty_output_is_never_overwritten(tmp_path):
    sentinel = tmp_path / 'sentinel'
    sentinel.write_bytes(b'unchanged')
    with pytest.raises(ValueError, match='overwrite'):
        package(tmp_path, tmp_path, tmp_path, tmp_path, '20261003-workflows-r2', sentinel)
    assert sentinel.read_bytes() == b'unchanged'


def test_missing_source_fails_before_output_creation(tmp_path):
    out = tmp_path / 'out'
    with pytest.raises(ValueError):
        package(tmp_path, tmp_path, tmp_path, out, '20261003-workflows-r2', tmp_path/'notes')
    assert not out.exists()


def test_local_source_runtime_and_sql_contracts():
    from pathlib import Path
    back = Path(__file__).resolve().parents[1]
    packs = prepare_sources(back.parent/'TS-frontend', back, back.parent/'TS-agent-console', '20261003-workflows-r2')
    assert len(packs) == 5
    for name, files, preserve in packs:
        if preserve:
            verify_checks(files)
        else:
            assert 'README.md' not in files
            assert not any('docs' in p.split('/') or 'tests' in p.split('/') for p in files)
            assert b'!.env.example' in files['.dockerignore']
        if name.startswith('ts-backend'):
            assert b'FROM python:3.12-slim' in files['Dockerfile']
            port = b'7018' if '-agent-console-' in name else b'7000'
            assert b'EXPOSE '+port in files['Dockerfile']
            assert 'app/api/routes/workflows.py' in files
    assert sha(packs[0][1]['requirements.txt']) == sha((back/'requirements.txt').read_bytes())
