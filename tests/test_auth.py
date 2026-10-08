from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_login_me_logout_flow() -> None:
    login = client.post(
        "/v1/auth/login",
        json={"username": "engineer01", "password": "change-me-before-demo"},
    )
    assert login.status_code == 200
    assert login.json()["user"]["username"] == "工程师01"

    me = client.get("/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["department"] == "电子电器架构部"

    logout = client.post("/v1/auth/logout")
    assert logout.status_code == 204
    assert client.get("/v1/auth/me").status_code == 401


def test_invalid_login() -> None:
    response = client.post(
        "/v1/auth/login",
        json={"username": "engineer01", "password": "wrong"},
    )
    assert response.status_code == 401
