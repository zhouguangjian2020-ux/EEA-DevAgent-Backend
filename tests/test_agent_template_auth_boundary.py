"""Offline regression for the existing UC template service-key boundary."""
import uuid

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import settings
from app.core.security import create_session_token
from app.main import app
from app.services.uc_template import uc_template_response

PATH = '/v1/agent/uc-template?scene=FROM_PRD_CREATE_UC'
CALLBACK_KEY = 'unit-test-only-callback-key-not-a-real-secret'
OUTBOUND_KEY = 'unit-test-only-outbound-key-not-a-real-secret'


@pytest.fixture(autouse=True)
def isolated_service_key(monkeypatch):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append('external-io')
        raise AssertionError('Template auth tests must not access DB or external HTTP')

    monkeypatch.setattr(settings, 'agent_callback_key', SecretStr(CALLBACK_KEY))
    monkeypatch.setattr(settings, 'agent_api_key', SecretStr(OUTBOUND_KEY))
    monkeypatch.setattr(settings, 'diagnostics_enabled', False)
    monkeypatch.setattr(psycopg, 'connect', forbidden)
    monkeypatch.setattr(psycopg.Connection, 'connect', forbidden)
    monkeypatch.setattr(psycopg.AsyncConnection, 'connect', forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', forbidden)
    yield
    assert calls == [], 'An endpoint attempted external IO even if it caught the error'


def assert_safe_error(response, status, code):
    assert response.status_code == status, response.text
    detail = response.json()['detail']
    assert detail['code'] == code
    request_id = response.headers['X-Request-ID']
    assert uuid.UUID(request_id).hex == request_id
    assert detail['request_id'] == request_id
    assert response.headers['Cache-Control'] == 'no-store'
    public = response.text + str(dict(response.headers))
    assert CALLBACK_KEY not in public
    assert OUTBOUND_KEY not in public
    assert 'set-cookie' not in response.headers


def test_correct_callback_key_works_without_user_session_or_external_io():
    with TestClient(app) as client:
        assert not client.cookies
        response = client.get(PATH, headers={'X-Agent-Internal-Key': CALLBACK_KEY})
    assert response.status_code == 200, response.text
    assert response.json() == uc_template_response().model_dump(mode='json')
    assert response.headers['Cache-Control'] == 'no-store'
    assert 'X-Request-ID' in response.headers
    assert 'set-cookie' not in response.headers
    assert CALLBACK_KEY not in response.text


@pytest.mark.parametrize('headers', [
    {},
    {'X-Agent-Internal-Key': ''},
    {'X-Agent-Internal-Key': 'wrong-key'},
    {'X-Agent-Internal-Key': OUTBOUND_KEY},
    {'X-API-Key': CALLBACK_KEY},
    {'X-API-Key': OUTBOUND_KEY},
    {'Authorization': 'Bearer ' + CALLBACK_KEY},
])
def test_missing_wrong_or_wrong_direction_credentials_are_rejected(headers):
    with TestClient(app) as client:
        response = client.get(PATH, headers=headers)
    assert_safe_error(response, 401, 'agent_unauthorized')


@pytest.mark.parametrize('configured', ['', '   '])
def test_unconfigured_callback_key_fails_closed(monkeypatch, configured):
    monkeypatch.setattr(settings, 'agent_callback_key', SecretStr(configured))
    with TestClient(app) as client:
        response = client.get(PATH, headers={'X-Agent-Internal-Key': CALLBACK_KEY})
    assert_safe_error(response, 503, 'agent_template_not_configured')


@pytest.mark.parametrize('headers', [{}, {'X-Agent-Internal-Key': 'wrong-key'}])
def test_valid_browser_session_cannot_replace_callback_key(headers):
    with TestClient(app) as client:
        client.cookies.set('ts_session', create_session_token(settings.demo_username))
        assert client.get('/v1/auth/me').status_code == 200
        response = client.get(PATH, headers=headers)
    assert_safe_error(response, 401, 'agent_unauthorized')


@pytest.mark.parametrize('path', ['/v1/auth/me', '/v1/projects'])
def test_callback_key_cannot_replace_browser_login(path):
    with TestClient(app) as client:
        response = client.get(path, headers={'X-Agent-Internal-Key': CALLBACK_KEY})
    assert response.status_code == 401, response.text
    assert CALLBACK_KEY not in response.text
    assert 'set-cookie' not in response.headers


def test_template_request_creates_no_reusable_browser_session():
    with TestClient(app) as client:
        response = client.get(PATH, headers={'X-Agent-Internal-Key': CALLBACK_KEY})
        assert response.status_code == 200
        assert not client.cookies
        assert client.get('/v1/auth/me').status_code == 401


def test_error_output_and_logs_do_not_echo_service_keys(caplog, capsys):
    with TestClient(app) as client:
        response = client.get(PATH, headers={'X-Agent-Internal-Key': OUTBOUND_KEY})
    assert_safe_error(response, 401, 'agent_unauthorized')
    captured = capsys.readouterr()
    output = captured.out + captured.err + caplog.text
    assert CALLBACK_KEY not in output
    assert OUTBOUND_KEY not in output
