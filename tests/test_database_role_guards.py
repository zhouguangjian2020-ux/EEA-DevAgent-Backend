"""Connection routing guards never accept the old login or a swapped read/write DSN."""
from unittest.mock import MagicMock

from pydantic import SecretStr
import pytest

from app.core.config import settings
from app.core.database import ProjectDatabaseNotConfigured, UnsafeProjectDatabaseRole, project_connection
from app.core.project_write_database import project_write_connection

DEFAULTS = ["default_transaction_read_only=off", "statement_timeout=5s", "lock_timeout=2s", "idle_in_transaction_session_timeout=15s"]


@pytest.mark.parametrize("factory,setting,name", [
    (project_connection,"projects_database_url","ee_project_api_ro"),
    (project_write_connection,"projects_write_database_url","ee_project_api_rw"),
])
@pytest.mark.parametrize("scenario", ["valid", "old_name", "swapped_name", "unsafe", "missing_role"])
def test_connection_identity_guard(monkeypatch, factory, setting, name, scenario):
    monkeypatch.setattr(settings,setting,SecretStr("host=localhost dbname=never-connected"))
    connection = MagicMock()
    connection.__enter__.return_value = connection
    role = {"name":name,"unsafe":False,"rolconfig":DEFAULTS}
    if scenario == "old_name":
        role["name"] = "ee_project_api_create"
    elif scenario == "swapped_name":
        role["name"] = "ee_project_api_rw" if name.endswith("ro") else "ee_project_api_ro"
    elif scenario == "unsafe":
        role["unsafe"] = True
    elif scenario == "missing_role":
        role = None
    connection.execute.return_value.fetchone.return_value = role
    monkeypatch.setattr("psycopg.connect",lambda *a,**kw: connection)
    if scenario == "valid":
        with factory() as result:
            assert result is connection
    else:
        with pytest.raises(UnsafeProjectDatabaseRole):
            with factory():
                pytest.fail("Unsafe connection yielded")


def test_writer_requires_reviewed_timeouts(monkeypatch):
    monkeypatch.setattr(settings,"projects_write_database_url",SecretStr("host=localhost dbname=never-connected"))
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = {"name":"ee_project_api_rw","unsafe":False,"rolconfig":[]}
    monkeypatch.setattr("psycopg.connect",lambda *a,**kw: connection)
    with pytest.raises(UnsafeProjectDatabaseRole):
        with project_write_connection():
            pytest.fail("Missing role limits accepted")


@pytest.mark.parametrize("factory,setting",[(project_connection,"projects_database_url"),(project_write_connection,"projects_write_database_url")])
def test_missing_dsn_never_connects(monkeypatch,factory,setting):
    monkeypatch.setattr(settings,setting,SecretStr(""))
    def forbidden(*args,**kwargs):
        pytest.fail("Attempted connection with no configured DSN")
    monkeypatch.setattr("psycopg.connect",forbidden)
    with pytest.raises(ProjectDatabaseNotConfigured):
        with factory():
            pytest.fail("Unconfigured connection yielded")
