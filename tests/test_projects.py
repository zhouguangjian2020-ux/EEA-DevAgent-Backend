from datetime import datetime, timezone
from uuid import UUID

from fastapi.testclient import TestClient
from pydantic import SecretStr
import psycopg
import pytest

from app.api.routes import projects as routes
from app.core.config import settings
from app.core.security import create_session_token
from app.main import app
from app.schemas.projects import ProjectListResponse, ProjectRead
from app.services import projects as service

PID = UUID('10d4c8a0-ff3a-56c3-abf6-18dc3b531e62')


def row(name='测试项目1'):
    return dict(id=PID, code='XRAY-DVR-SAMPLE', name=name, description=None,
                lock_version='9007199254740993', created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc))


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, 'projects_database_url', SecretStr(''))
    with TestClient(app) as c:
        c.cookies.set('ts_session', create_session_token(settings.demo_username))
        yield c


@pytest.mark.parametrize('path',['/v1/projects',f'/v1/projects/{PID}'])
def test_auth_required_before_database(client, monkeypatch, path):
    monkeypatch.setattr(service,'project_connection',lambda: pytest.fail('Unauthorized database access'))
    client.cookies.clear()
    r=client.get(path)
    assert r.status_code==401
    assert r.headers['Cache-Control']=='no-store'


def test_cookie_not_bearer_authorization(client):
    client.cookies.clear()
    r=client.get('/v1/projects',headers={'Authorization':'Bearer '+create_session_token(settings.demo_username)})
    assert r.status_code==401


def test_not_configured_is_sanitized_503(client):
    r=client.get('/v1/projects')
    assert r.status_code==503
    assert r.json()['detail']['code']=='projects_not_configured'
    assert r.json()['detail']['request_id']==r.headers['X-Request-ID']


@pytest.mark.parametrize('params',[{'limit':0},{'limit':101},{'offset':-1},{'offset':100001},{'limit':'abc'}])
def test_invalid_pagination_does_not_query(client,monkeypatch,params):
    monkeypatch.setattr(service,'project_connection',lambda: pytest.fail('Invalid query reached DB'))
    assert client.get('/v1/projects',params=params).status_code==422


def test_client_cannot_select_another_subject(client,monkeypatch):
    called=[]
    def listing(subject,limit,offset):
        called.append((subject,limit,offset))
        return ProjectListResponse(items=[ProjectRead(**row())],limit=limit,offset=offset,has_more=False)
    monkeypatch.setattr(routes,'list_projects',listing)
    r=client.get('/v1/projects?subject=victim&user_id=victim&limit=2')
    assert r.status_code==200 and called==[('engineer01',2,0)]
    assert r.json()['items'][0]['lock_version']=='9007199254740993'
    assert 'metadata' not in r.json()['items'][0]
    assert r.headers['Cache-Control']=='no-store'


def test_pagination_uses_extra_row_and_bound_parameters(monkeypatch):
    called=[]
    def read(sql,params):
        called.append((sql,params));return [row(),row('other')]
    monkeypatch.setattr(service,'_read',read)
    result=service.list_projects("x' OR TRUE --",1,3)
    assert len(result.items)==1 and result.has_more
    assert called[0][1]==("x' OR TRUE --",2,3)
    assert "x' OR TRUE --" not in called[0][0]
    assert 'EXISTS' in called[0][0]


def test_missing_and_unauthorized_project_share_404(client,monkeypatch):
    monkeypatch.setattr(service,'_read',lambda *_: [])
    r=client.get(f'/v1/projects/{PID}')
    assert r.status_code==404 and r.json()['detail']['code']=='project_not_found'
    assert r.headers['Cache-Control']=='no-store'


def test_detail_query_parameterized(monkeypatch):
    captured=[]
    monkeypatch.setattr(service,'_read',lambda sql,params: captured.append((sql,params)) or [row()])
    result=service.get_project('engineer01',PID)
    assert result.id==PID and captured[0][1]==('engineer01',PID)
    assert str(PID) not in captured[0][0]



def test_workspace_auth_required_before_database(client, monkeypatch):
    monkeypatch.setattr(routes, "get_project_workspace", lambda *_: pytest.fail("Unauthorized database access"))
    client.cookies.clear()
    response = client.get(f"/v1/projects/{PID}/workspace")
    assert response.status_code == 401
    assert response.headers["Cache-Control"] == "no-store"


def test_workspace_route_uses_authenticated_subject(client, monkeypatch):
    from app.schemas.project_workspace import ProjectWorkspaceResponse
    called = []
    monkeypatch.setattr(routes, "get_project_workspace", lambda subject, project_id: called.append((subject, project_id)) or ProjectWorkspaceResponse(project=row(), functions=[]))
    response = client.get(f"/v1/projects/{PID}/workspace?subject=victim")
    assert response.status_code == 200
    assert called == [("engineer01", PID)]
    assert response.json()["read_only"] is True


def test_workspace_db_error_is_sanitized(client, monkeypatch):
    monkeypatch.setattr(routes, "get_project_workspace", lambda *_: (_ for _ in ()).throw(service.ProjectReadError(503, "projects_database_unavailable", "项目数据库暂不可用")))
    response = client.get(f"/v1/projects/{PID}/workspace")
    assert response.status_code == 503
    assert "password" not in response.text.lower()
def test_workspace_scenario_slots_use_fixed_category_order():
    from app.services.project_workspace import _BRANCH_ORDER, _EXCEPTION_ORDER, _scenario_bodies
    rows = [
        {"category": "a3", "position": 0, "item_id": UUID(int=3), "body": "A3"},
        {"category": "a1", "position": 9, "item_id": UUID(int=1), "body": "A1"},
        {"category": "a2", "position": 0, "item_id": UUID(int=2), "body": "A2"},
    ]
    exception_rows = [
        {"category": "power", "position": 0, "item_id": UUID(int=5), "body": "P"},
        {"category": "vehicle_state", "position": 0, "item_id": UUID(int=1), "body": "V"},
    ]
    assert _scenario_bodies(rows, _BRANCH_ORDER, 4) == ["A1", "A2", "A3", ""]
    assert _scenario_bodies(exception_rows, _EXCEPTION_ORDER, 5) == ["V", "", "", "", "P"]


def test_workspace_scenario_slots_preserve_missing_middle_and_empty_categories():
    from app.services.project_workspace import _BRANCH_ORDER, _EXCEPTION_ORDER, _scenario_bodies
    rows = [
        {"category": "a3", "position": 0, "item_id": UUID(int=3), "body": "  第三分支\n原文  "},
        {"category": "a1", "position": 1, "item_id": UUID(int=1), "body": "第一分支"},
    ]
    assert _scenario_bodies(rows, _BRANCH_ORDER, 4) == ["第一分支", "", "  第三分支\n原文  ", ""]
    assert _scenario_bodies([], _EXCEPTION_ORDER, 5) == ["", "", "", "", ""]


def test_workspace_service_serializes_validated_project(monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from app.services import project_workspace as workspace

    project = row()

    class Connection:
        calls = 0

        def execute(self, statement, params):
            self.calls += 1
            rows = [project] if self.calls == 1 else []
            return SimpleNamespace(fetchall=lambda: rows)

    snapshot_modes = []

    @contextmanager
    def connection(*, repeatable_read=False):
        snapshot_modes.append(repeatable_read)
        yield Connection()

    monkeypatch.setattr(workspace, "project_connection", connection)
    result = workspace.get_project_workspace("engineer01", PID).model_dump(mode="json")
    assert result["project"] == ProjectRead.model_validate(project).model_dump(mode="json")
    assert result["project"]["lock_version"] == "9007199254740993"
    assert result["functions"] == []
    assert result["read_only"] is True
    assert snapshot_modes == [True]


def test_invalid_uuid_is_422(client):
    assert client.get('/v1/projects/not-a-uuid').status_code==422


@pytest.mark.parametrize('method',['POST','PUT','PATCH','DELETE'])
@pytest.mark.parametrize('path',['/v1/projects',f'/v1/projects/{PID}',f'/v1/projects/{PID}/workspace'])
def test_no_write_routes(client,method,path):
    expected = 403 if (method == 'POST' and path == '/v1/projects') or (method in ('PATCH', 'DELETE') and path == f'/v1/projects/{PID}') else 405
    assert client.request(method,path,json={'name':'forbidden'}).status_code==expected


@pytest.mark.parametrize('error,status,code',[
    (psycopg.OperationalError('password=secret host=private'),503,'projects_database_unavailable'),
    (psycopg.errors.QueryCanceled('private SQL'),504,'projects_query_timeout'),
    (psycopg.errors.LockNotAvailable('private lock'),504,'projects_query_timeout'),
    (ValueError('secret DSN'),503,'projects_database_unavailable'),
])
def test_database_errors_sanitized(client,monkeypatch,error,status,code):
    def fail(): raise error
    monkeypatch.setattr(service,'project_connection',fail)
    r=client.get('/v1/projects')
    assert r.status_code==status and r.json()['detail']['code']==code
    assert all(x not in r.text for x in ['secret','private','password='])


def test_project_route_allowlist_through_stage_four(client):
    paths=client.get('/openapi.json').json()['paths']
    actual={path:list(value) for path,value in paths.items() if path.startswith('/v1/projects')}
    workflow_paths={
        '/v1/projects/{project_id}/prd-creations/plan':['post'],
        '/v1/projects/{project_id}/prd-creations/intents':['post','get'],
        '/v1/projects/{project_id}/prd-creations/intents/{intent_id}':['get'],
        '/v1/projects/{project_id}/prd-creations/intents/{intent_id}/execute':['post'],
        '/v1/projects/{project_id}/functions/{function_id}/fr-generations':['post'],
        '/v1/projects/{project_id}/functions/{function_id}/uc-generations/append':['post'],
        '/v1/projects/{project_id}/functions/{function_id}/fr-generations/append':['post'],
        '/v1/projects/{project_id}/functions/{function_id}/allocation-generations':['post'],
        '/v1/projects/{project_id}/functions/{function_id}/fr-candidates/adopt':['post'],
        '/v1/projects/{project_id}/functions/{function_id}/allocations':['post'],
        '/v1/projects/{project_id}/allocations/{allocation_id}/accept':['post'],
        '/v1/projects/{project_id}/conversations':['get','post'],
        '/v1/projects/{project_id}/conversations/{thread_id}':['get'],
        '/v1/projects/{project_id}/conversations/{thread_id}/tasks':['get'],
        '/v1/projects/{project_id}/conversations/{thread_id}/messages/stream':['post'],
    }
    for path,methods in workflow_paths.items():assert actual.pop(path)==methods
    assert actual=={'/v1/projects':['get','post'],'/v1/projects/{project_id}':['get','patch','delete'],'/v1/projects/{project_id}/workspace':['get'],
        '/v1/projects/{project_id}/functions':['post'],
        '/v1/projects/{project_id}/functions/{function_id}':['patch','delete'],
        '/v1/projects/{project_id}/functions/{function_id}/artifacts':['post'],
        '/v1/projects/{project_id}/artifacts/{artifact_id}/revisions':['post'],
        '/v1/projects/{project_id}/artifacts/{artifact_id}/accept':['post'],
        '/v1/projects/{project_id}/deletion-impact':['get'],
        '/v1/projects/{project_id}/functions/{function_id}/deletion-impact':['get'],
        '/v1/projects/{project_id}/artifacts/{artifact_id}/deletion-impact':['get'],
        '/v1/projects/{project_id}/artifacts/{artifact_id}':['delete'],
        '/v1/projects/{project_id}/deletion-batch':['post'],
        '/v1/projects/{project_id}/deletion-batch/impact':['post'],
        '/v1/projects/{project_id}/permissions':['get'],
        '/v1/projects/{project_id}/permission-candidates':['get'],
        '/v1/projects/{project_id}/members':['post'],
        '/v1/projects/{project_id}/members/{user_id}':['delete'],
        '/v1/projects/{project_id}/functions/{function_id}/permissions':['post'],
        '/v1/projects/{project_id}/functions/{function_id}/permissions/{user_id}':['delete']}

