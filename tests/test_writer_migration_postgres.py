"""Migration safety tests. Opt-in on a fresh localhost fixture BEFORE applying the package.

Every test rolls back its catalog/data changes. No real credentials or remote DSNs.
"""
import os
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict
import pytest

BUNDLE = Path(__file__).resolve().parents[2] / "TS-frontend/docs/current/database/manual-work-20260923"


def fragment(name):
    return (BUNDLE / name).read_text(encoding="utf8")


@pytest.fixture
def db():
    dsn = os.getenv("TEST_MIGRATION_ADMIN_DSN")
    if not dsn:
        pytest.skip("Requires the disposable pre-migration localhost PostgreSQL fixture")
    info = conninfo_to_dict(dsn)
    ports = {"56428", "56429", "56431"} if os.getenv("TEST_TASK_RECORDS_ENABLED") == "1" else {"56428", "56429"}
    assert info.get("host") == "127.0.0.1" and info.get("port") in ports
    assert info.get("dbname", "").startswith(("ee_manual_", "ee_project_read_"))
    with psycopg.connect(dsn, autocommit=True) as connection:
        assert connection.execute("SELECT system_identifier::text FROM pg_control_system()").fetchone()[0] != "7686454253092706426"
        with connection.transaction(force_rollback=True):
            yield connection


def apply_fragments(connection):
    connection.execute("SET LOCAL ee.manual_migration='writer-rename-r2'")
    connection.execute(fragment("004_verify_reader.sql"))
    connection.execute(fragment("001_business_writer.sql"))
    connection.execute(fragment("002_verify.sql"))
    connection.execute(fragment("004_verify_reader.sql"))


def migration_state(connection):
    # No password/verifier is read into test output, even on an assertion failure.
    return (
        connection.execute("SELECT rolname,oid FROM pg_roles WHERE rolname IN ('ee_project_api_create','ee_project_api_rw') ORDER BY rolname").fetchall(),
        connection.execute("SELECT count(*) FROM ee.access_roles WHERE code='project_api_author'").fetchone(),
        connection.execute("SELECT to_regprocedure('ee.accept_manual_revision(uuid,uuid,uuid,uuid,text)')::text").fetchone(),
        connection.execute("SELECT grantee,table_name,privilege_type,is_grantable FROM information_schema.table_privileges WHERE table_schema='ee' AND grantee IN ('ee_project_api_create','ee_project_api_rw') ORDER BY 1,2,3").fetchall(),
    )


def reject_unchanged(connection, pattern):
    before = migration_state(connection)
    with pytest.raises(psycopg.errors.RaiseException, match=pattern):
        with connection.transaction():
            apply_fragments(connection)
    assert migration_state(connection) == before


def test_in_place_identity_and_exact_grants(db):
    old_oid = db.execute("SELECT oid FROM pg_roles WHERE rolname='ee_project_api_create'").fetchone()[0]
    apply_fragments(db)
    assert db.execute("SELECT oid FROM pg_roles WHERE rolname='ee_project_api_rw'").fetchone()[0] == old_oid
    assert db.execute("SELECT count(*) FROM pg_roles WHERE rolname='ee_project_api_create'").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM information_schema.table_privileges WHERE grantee='ee_project_api_rw' AND privilege_type='SELECT'").fetchone()[0] == 24
    assert db.execute("SELECT count(*) FROM information_schema.table_privileges WHERE grantee='ee_project_api_rw' AND privilege_type='INSERT'").fetchone()[0] == 16
    assert db.execute("SELECT has_column_privilege('ee_project_api_rw','ee.artifacts','head_revision_id','UPDATE'),has_column_privilege('ee_project_api_rw','ee.artifacts','accepted_revision_id','UPDATE')").fetchone() == (True, False)


def test_new_name_conflict_is_not_merged(db):
    db.execute("CREATE ROLE ee_project_api_rw NOLOGIN")
    reject_unchanged(db, "New writer already exists")


def test_missing_old_role_is_not_recreated(db):
    db.execute("ALTER ROLE ee_project_api_create RENAME TO isolated_missing_writer")
    reject_unchanged(db, "Old writer missing")


@pytest.mark.parametrize("attribute", ["INHERIT", "NOLOGIN", "SUPERUSER", "CREATEDB", "CREATEROLE", "REPLICATION", "BYPASSRLS"])
def test_unsafe_attributes_are_not_repaired(db, attribute):
    db.execute("ALTER ROLE ee_project_api_create " + attribute)
    reject_unchanged(db, "attributes are unsafe")


@pytest.mark.parametrize("grant", ["GRANT ee_m0_owner TO ee_project_api_create", "GRANT ee_project_api_create TO ee_m0_owner"])
def test_both_membership_directions_are_rejected(db, grant):
    db.execute(grant)
    reject_unchanged(db, "memberships")


@pytest.mark.parametrize("clause", ["PASSWORD NULL", "PASSWORD 'md5" + "0"*32 + "'", "VALID UNTIL '2000-01-01'"])
def test_incompatible_or_expired_credentials_stop_before_rename(db, clause):
    db.execute("ALTER ROLE ee_project_api_create " + clause)
    reject_unchanged(db, "SCRAM password required|expired")


@pytest.mark.parametrize("grant", [
    "GRANT DELETE ON ee.projects TO ee_project_api_create",
    "GRANT UPDATE(name) ON ee.projects TO ee_project_api_create",
    "GRANT SELECT ON ee.projects TO ee_project_api_create WITH GRANT OPTION",
    "REVOKE INSERT ON ee.projects FROM ee_project_api_create",
    "GRANT CREATE ON SCHEMA ee TO ee_project_api_create",
    "GRANT EXECUTE ON FUNCTION ee.controlled_import() TO ee_project_api_create",
])
def test_old_permission_drift_is_not_silently_repaired(db, grant):
    db.execute(grant)
    reject_unchanged(db, "privilege|authority")


def test_changed_role_defaults_are_rejected(db):
    db.execute("ALTER ROLE ee_project_api_create RESET lock_timeout")
    reject_unchanged(db, "defaults")


def test_live_writer_connection_blocks_rename(db):
    dsn = os.environ["TEST_MIGRATION_WRITER_DSN"]
    info = conninfo_to_dict(dsn)
    ports = {"56428", "56429", "56431"} if os.getenv("TEST_TASK_RECORDS_ENABLED") == "1" else {"56428", "56429"}
    assert info["host"] == "127.0.0.1" and info["port"] in ports
    assert info["dbname"].startswith(("ee_manual_", "ee_project_read_"))
    with psycopg.connect(dsn, autocommit=True) as writer:
        assert writer.execute("SELECT current_user").fetchone()[0] == "ee_project_api_create"
        reject_unchanged(db, "sessions still connected")


def test_late_reader_verification_failure_rolls_back_rename_and_grants(db):
    before = migration_state(db)
    with pytest.raises(psycopg.errors.RaiseException, match="reader column"):
        with db.transaction():
            db.execute("SET LOCAL ee.manual_migration='writer-rename-r2'")
            db.execute(fragment("001_business_writer.sql"))
            db.execute(fragment("002_verify.sql"))
            db.execute("GRANT UPDATE(name) ON ee.projects TO ee_project_api_ro")
            db.execute(fragment("004_verify_reader.sql"))
    assert migration_state(db) == before
    db.execute(fragment("004_verify_reader.sql"))


def test_author_permission_failure_rolls_back_rename(db):
    db.execute("UPDATE ee.permission_definitions SET enabled=false WHERE code='project.read'")
    before = migration_state(db)
    with pytest.raises(psycopg.errors.RaiseException, match="project.read"):
        with db.transaction():
            db.execute("SET LOCAL ee.manual_migration='writer-rename-r2'")
            db.execute(fragment("001_business_writer.sql"))
            db.execute(fragment("002_verify.sql"))
    assert migration_state(db) == before


def test_repeat_is_safely_rejected_and_verify_is_repeatable(db):
    apply_fragments(db)
    reject_unchanged(db, "Old writer missing")
    db.execute(fragment("002_verify.sql"))
    db.execute(fragment("004_verify_reader.sql"))


def test_fragment_refuses_accidental_standalone_execution(db):
    with pytest.raises(psycopg.errors.RaiseException, match="Use apply.sql"):
        with db.transaction():
            db.execute(fragment("001_business_writer.sql"))


@pytest.mark.parametrize("wrong", ["database", "system_id"])
def test_target_guard_rejects_wrong_database_or_cluster(db, wrong):
    database = db.execute("SELECT current_database()").fetchone()[0]
    system_id = db.execute("SELECT system_identifier::text FROM pg_control_system()").fetchone()[0]
    target = fragment("005_target.sql").replace(":'expected_database'", "'wrong_database'" if wrong=="database" else "'"+database+"'")
    target = target.replace(":'expected_system_id'", "'0'" if wrong=="system_id" else "'"+system_id+"'")
    before = migration_state(db)
    with pytest.raises(psycopg.errors.RaiseException, match="Wrong"):
        with db.transaction():
            db.execute(target)
    assert migration_state(db) == before


def test_other_database_grant_blocks_cluster_wide_rename(db):
    db.execute("GRANT CONNECT ON DATABASE postgres TO ee_project_api_create")
    reject_unchanged(db, "another database")
