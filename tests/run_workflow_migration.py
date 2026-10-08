"""Extra safety checks for the NEW workflow bundle, disposable localhost only."""
import psycopg
from psycopg.conninfo import conninfo_to_dict


def verify_migration(command, psql, target, package, admin, snapshot, expected_port=56429):
    info = conninfo_to_dict(admin)
    assert expected_port in (56429, 56431)
    assert info['host'] == '127.0.0.1' and info['port'] == str(expected_port)
    assert info['dbname'].startswith('ee_manual_')
    with psycopg.connect(admin) as db:
        before = snapshot(db)
    for name, extra, entry in (
        ('database', ['-v', 'expected_database=wrong_workflow_database'], 'apply.sql'),
        ('cluster', ['-v', 'expected_system_id=0'], 'apply.sql'),
        ('fragment', [], '001_workflows.sql'),
    ):
        assert command([*psql, *target, *extra, '-f', package / entry], 'workflow-reject-' + name + '.log') != 0, name
        with psycopg.connect(admin) as db:
            assert snapshot(db) == before
            assert db.execute("SELECT to_regclass('ee.chat_threads')").fetchone()[0] is None
            assert db.execute("SELECT count(*) FROM ee.permission_definitions WHERE code LIKE 'fr_allocation.%' AND action IN ('create','update','accept')").fetchone()[0] == 0
    for entry in ('apply.sql', 'verify.sql'):
        assert command([*psql, *target, '-f', package / entry], 'workflow-' + entry + '.log') == 0, entry
    assert command([*psql, *target, '-f', package / 'apply.sql'], 'workflow-repeat-rejected.log') != 0
    with psycopg.connect(admin) as db:
        # Simulate a later custom-role grant. Rollback must not silently remove it.
        row = db.execute("INSERT INTO ee.role_permissions(role_id,permission_code) SELECT id,'fr_allocation.create' FROM ee.access_roles WHERE code='project_api_reader' RETURNING role_id").fetchone()
        assert row is not None
    assert command([*psql, *target, '-f', package / 'rollback-empty-only.sql'], 'workflow-later-grant-rollback-rejected.log') != 0
    with psycopg.connect(admin) as db:
        assert db.execute("SELECT 1 FROM ee.role_permissions WHERE role_id=%s AND permission_code='fr_allocation.create'", (row[0],)).fetchone()
        db.execute("DELETE FROM ee.role_permissions WHERE role_id=%s AND permission_code='fr_allocation.create'", (row[0],))
    assert command([*psql, *target, '-f', package / 'rollback-empty-only.sql'], 'workflow-empty-rollback.log') == 0
    with psycopg.connect(admin) as db:
        assert snapshot(db) == before
        assert not db.execute("SELECT has_table_privilege('ee_project_api_rw','ee.allocation_targets','INSERT')").fetchone()[0]
    for entry in ('apply.sql', 'verify.sql'):
        assert command([*psql, *target, '-f', package / entry], 'workflow-reapply-' + entry + '.log') == 0
    return 'wrong database/cluster/direct fragment/repeat rejected; later grants protected; empty rollback and reapply verified'


def verify_data_rollback(command, psql, target, package, admin, snapshot, verification_package=None):
    with psycopg.connect(admin) as db:
        assert db.execute('SELECT count(*) FROM ee.chat_threads').fetchone()[0] > 0
        before = snapshot(db)
    assert command([*psql, *target, '-f', package / 'rollback-empty-only.sql'], 'workflow-data-rollback-rejected.log') != 0
    assert command([*psql, *target, '-f', (verification_package or package) / 'verify.sql'], 'workflow-final-verify.log') == 0
    with psycopg.connect(admin) as db:
        assert snapshot(db) == before
