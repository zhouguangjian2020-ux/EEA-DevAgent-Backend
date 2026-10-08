"""Bounded, read-only connections for project reads. No pool, schema creation or fallback."""
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from app.core.config import settings


class ProjectDatabaseNotConfigured(Exception):
    pass


class UnsafeProjectDatabaseRole(Exception):
    pass


@contextmanager
def project_connection(*, repeatable_read=False):
    dsn = settings.projects_database_url.get_secret_value().strip()
    if not dsn:
        raise ProjectDatabaseNotConfigured()
    with psycopg.connect(
        dsn,
        connect_timeout=settings.projects_connect_timeout_seconds,
        options="-c default_transaction_read_only=on -c statement_timeout=5000 -c lock_timeout=2000",
        application_name="ts-project-read-api",
        row_factory=dict_row,
    ) as connection:
        connection.read_only = True
        if repeatable_read:
            connection.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        from app.core.diagnostics import observe_connection
        observed = observe_connection(connection)
        role = observed.execute(
            "SELECT current_user AS name, NOT r.rolcanlogin OR r.rolinherit OR "
            "r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication OR r.rolbypassrls "
            "OR EXISTS (SELECT 1 FROM pg_auth_members m WHERE m.member=r.oid OR m.roleid=r.oid) AS unsafe "
            "FROM pg_roles r WHERE r.rolname = current_user"
        ).fetchone()
        if role is None or role["name"] != "ee_project_api_ro" or role["unsafe"]:
            raise UnsafeProjectDatabaseRole()
        yield observed
