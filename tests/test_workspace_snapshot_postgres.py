"""Opt-in local PostgreSQL proof that workspace reads use one repeatable-read snapshot."""
import os
from uuid import uuid4

import psycopg
from psycopg.conninfo import conninfo_to_dict
from pydantic import SecretStr
import pytest

from app.core.config import settings
from app.core.database import project_connection


def _dsn(primary: str, fallback: str) -> str | None:
    return os.environ.get(primary) or os.environ.get(fallback)


@pytest.fixture
def snapshot_db(monkeypatch):
    admin_dsn = _dsn("TEST_PROJECTS_SNAPSHOT_ADMIN_DSN", "TEST_MANUAL_ADMIN_DSN")
    reader_dsn = _dsn("TEST_PROJECTS_SNAPSHOT_READER_DSN", "TEST_MANUAL_READER_DSN")
    if not admin_dsn or not reader_dsn:
        pytest.skip("Requires an explicit disposable local PostgreSQL snapshot fixture")
    for dsn in (admin_dsn, reader_dsn):
        info = conninfo_to_dict(dsn)
        assert info.get("host") == "127.0.0.1"
        assert info.get("port") in {"56431", "56432"}
        assert info.get("dbname", "").startswith(("ee_manual_", "ee_project_read_"))
    monkeypatch.setattr(settings, "projects_database_url", SecretStr(reader_dsn))
    yield admin_dsn


def test_project_connection_repeatable_read_keeps_workspace_snapshot(snapshot_db):
    """A concurrent committed row must not appear halfway through one workspace read."""
    table = "snapshot_probe_" + uuid4().hex
    quoted = '"' + table + '"'
    with psycopg.connect(snapshot_db, autocommit=True) as admin:
        admin.execute(f"CREATE TABLE {quoted} (id integer PRIMARY KEY)")
        admin.execute(f"GRANT SELECT ON {quoted} TO ee_project_api_ro")
        try:
            with project_connection(repeatable_read=True) as first_read:
                assert first_read.execute(f"SELECT count(*) AS count FROM {quoted}").fetchone()["count"] == 0
                admin.execute(f"INSERT INTO {quoted} (id) VALUES (1)")
                assert first_read.execute(f"SELECT count(*) AS count FROM {quoted}").fetchone()["count"] == 0
        finally:
            admin.execute(f"DROP TABLE {quoted}")
