"""Offline package verification. Never uses company services or starts daemons."""
import argparse
import ast
import json
import os
from pathlib import Path
import subprocess
import sys

from package_workflow_release import inspect_archive, sha, verify_checks

ROUTES = {
    '/v1/projects/{project_id}/functions/{function_id}/fr-generations': 'post',
    '/v1/projects/{project_id}/functions/{function_id}/fr-candidates/adopt': 'post',
    '/v1/projects/{project_id}/functions/{function_id}/allocation-generations': 'post',
    '/v1/projects/{project_id}/functions/{function_id}/allocations': 'post',
    '/v1/projects/{project_id}/allocations/{allocation_id}/accept': 'post',
    '/v1/projects/{project_id}/conversations': 'post',
    '/v1/projects/{project_id}/conversations/{thread_id}': 'get',
    '/v1/projects/{project_id}/conversations/{thread_id}/messages/stream': 'post',
}


def run(release: Path, evidence: Path, gnu: Path):
    if evidence.exists():
        raise ValueError('Use a new verification directory')
    evidence.mkdir(parents=True)
    top = {p.name: p.read_bytes() for p in release.iterdir() if p.is_file()}
    verify_checks(top)
    manifest = json.loads(top['manifest.json'])
    assert len(manifest['archives']) == 5
    report = {'status': 'RUNNING', 'company_connected': False, 'docker_built': False,
              'root_manifest_sha256': sha(top['SHA256SUMS']), 'archives': []}
    def check(command, cwd, filename, env=None):
        result = subprocess.run([str(a) for a in command], cwd=cwd, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        (evidence / filename).write_bytes(result.stdout)
        if result.returncode:
            raise RuntimeError(f'{filename} failed; inspect local evidence')
    check([gnu, '-c', 'SHA256SUMS'], release, 'root-gnu-sha256.log')
    extracted = {}
    for item in manifest['archives']:
        blob = top[item['name']]
        assert sha(blob) == item['sha256']
        files = inspect_archive(blob, item['root'])
        target = evidence / item['root']
        target.mkdir()
        for relative, data in files.items():
            path = target / relative
            assert path.resolve().is_relative_to(target.resolve())
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            if path.suffix == '.py':
                ast.parse(data, filename=relative)
        check([gnu, '-c', 'SHA256SUMS'], target, item['root'] + '-gnu-sha256.log')
        sql = item['root'].startswith('ts-db-')
        if not sql:
            assert not any(any(p in {'docs','tests','tmp','artifacts','.venv','node_modules','data','__pycache__'} for p in Path(n).parts) for n in files)
            assert not any(Path(n).name == '.env' or Path(n).name.startswith('.env.') and not n.endswith('.env.example') for n in files)
        extracted[item['root']] = target
        report['archives'].append({'archive': item['name'], 'entries': len(files), 'gnu_sha256': 'PASS'})
    safe = {k: v for k, v in os.environ.items() if not k.startswith(('PG','TEST_','PROJECTS_','BUSINESS_','AGENT_','DIAGNOSTICS_','LAB_','SECRET_','DEMO_','PYTHONPATH'))}
    safe.update(PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8', APP_ENV='development',
                SECRET_KEY='offline-release-verification-only', DEMO_USERNAME='engineer01',
                DEMO_PASSWORD='offline-verification-only', COOKIE_SECURE='false',
                PROJECTS_DATABASE_URL='', PROJECTS_WRITE_DATABASE_URL='', BUSINESS_WRITES_ENABLED='false',
                AGENT_INVOKE_URL='', AGENT_GENERATION_INVOKE_URL='', AGENT_API_KEY='', AGENT_CALLBACK_KEY='',
                DIAGNOSTICS_ENABLED='false', DIAGNOSTICS_KEY='', DIAGNOSTICS_PROJECT_IDS='')
    code = '''
import json,sys
from fastapi.testclient import TestClient
from app.main import app
assert sys.version_info >= (3,12)
required=json.loads(sys.argv[1])
with TestClient(app) as client:
    for path in ['/health','/docs','/redoc']:
        assert client.get(path).status_code == 200, path
    assert client.get('/v1/auth/me').status_code == 401
    paths=client.get('/openapi.json').json()['paths']
    for path,method in required.items():
        assert method in paths[path], path
        print('PRESENT',method.upper(),path)
    response=client.post('/v1/auth/login',json={'username':'engineer01','password':'offline-verification-only'})
    assert response.status_code == 200
    assert client.get('/v1/auth/me').status_code == 200
    assert client.post('/v1/auth/logout').status_code == 204
    assert client.get('/v1/auth/me').status_code == 401
print('EXTRACTED_BACKEND_SMOKE_PASS; no database/Agent calls')
'''
    for name, target in extracted.items():
        if name.startswith('ts-backend-'):
            check([sys.executable, '-c', code, json.dumps(ROUTES)], target, name + '-smoke.log', safe)
        elif name.startswith('ts-agent-console-'):
            env = {**safe, 'LAB_DEMO_MODE': 'true', 'LAB_DATA_DIR': str(evidence/'console-test-data')}
            check([sys.executable, '-c', '''
from fastapi.testclient import TestClient
from labconsole.main import app
from labconsole.cases import CASES
assert len(CASES)==9
assert {'fr-generation','allocation-generation','chat-thread','project-chat'} <= set(CASES)
with TestClient(app) as client:
    assert client.get('/health').status_code==200
    assert client.get('/').status_code==200
    assert client.get('/api/info').json()['demo'] is True
print('EXTRACTED_CONSOLE_SMOKE_PASS; 9 cases; no external requests')
'''], target, name + '-smoke.log', env)
    report.update(status='PASS', backend_openapi_paths=8, backend_smoke='both archives passed', console_cases=9,
                  python_syntax='PASS', manifests='GNU sha256sum verified root and all five inner manifests')
    (evidence/'verification.json').write_bytes((json.dumps(report, indent=2)+'\n').encode('utf-8'))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--gnu-sha256sum', type=Path, required=True)
    a=parser.parse_args()
    run(a.release.resolve(), a.evidence.resolve(), a.gnu_sha256sum.resolve())
