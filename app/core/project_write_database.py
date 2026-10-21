"""Shared, narrowly granted business writer connection. Never upgrade the reader DSN."""
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from app.core.config import settings
from app.core.database import ProjectDatabaseNotConfigured, UnsafeProjectDatabaseRole


@contextmanager
def project_write_connection(*, read_only=False):
    dsn = settings.projects_write_database_url.get_secret_value().strip()
    if not dsn:
        raise ProjectDatabaseNotConfigured()
    options = "-c statement_timeout=5000 -c lock_timeout=2000 -c idle_in_transaction_session_timeout=15000"
    if read_only:
        options += " -c default_transaction_read_only=on"
    with psycopg.connect(
        dsn, connect_timeout=settings.projects_connect_timeout_seconds,
        options=options,
        application_name="ts-business-receipt-api" if read_only else "ts-business-write-api", row_factory=dict_row,
    ) as connection:
        # Set before the very first query; the dedicated role remains checked.
        if read_only:
            connection.read_only = True
            connection.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        role = connection.execute(
            "SELECT current_user AS name, r.rolconfig, NOT r.rolcanlogin OR r.rolinherit OR "
            "r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication OR r.rolbypassrls OR "
            "EXISTS (SELECT 1 FROM pg_auth_members m WHERE m.member=r.oid OR m.roleid=r.oid) AS unsafe "
            "FROM pg_roles r WHERE r.rolname = current_user"
        ).fetchone()
        required_role_settings = {
            "default_transaction_read_only=off",
            "statement_timeout=5s",
            "lock_timeout=2s",
            "idle_in_transaction_session_timeout=15s",
        }
        if (
            role is None
            or role["name"] != "ee_project_api_rw"
            or role["unsafe"]
            or not required_role_settings.issubset(set(role["rolconfig"] or []))
        ):
            raise UnsafeProjectDatabaseRole()
        from app.core.diagnostics import observe_connection
        yield observe_connection(connection)
        # psycopg commits only after the complete service operation; any error rolls back.
