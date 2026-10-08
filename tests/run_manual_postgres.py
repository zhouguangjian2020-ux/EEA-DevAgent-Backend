"""Disposable localhost PG18 only. Never loads private .env or contacts company services.

--legacy runs prior read/project-create regressions with the new role policy and
BUSINESS_WRITES_ENABLED=false, not with the obsolete database login.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys

import psycopg
from psycopg import sql as pgsql
from psycopg.conninfo import make_conninfo

BACK = Path(__file__).resolve().parents[1]
FRONT = BACK.parent / "TS-frontend"
BASE = FRONT / "docs/current/database/m0-20260921-r2"
READ = FRONT / "docs/current/database/test-project-read-workspace-20260922"
OLD = FRONT / "docs/current/database/project-create-20260922"
NEW = FRONT / "docs/current/database/manual-work-20260923"
PG = FRONT / "tmp/metadata-pg18-r2/pgsql/bin"
FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)
COMPANY_SYSTEM_ID = "7686454253092706426"


def check_bundle(folder):
    for line in (folder / "SHA256SUMS").read_text(encoding="utf8").splitlines():
        expected, name = line.split(maxsplit=1)
        path = (folder / name).resolve()
        if not path.is_relative_to(folder.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError(f"Bundle checksum mismatch: {folder.name}/{name}")


def business_snapshot(connection):
    tables = connection.execute("SELECT tablename FROM pg_tables WHERE schemaname='ee' "
        "AND tablename NOT IN ('permission_definitions','access_roles','role_permissions') ORDER BY tablename").fetchall()
    return {name: connection.execute(pgsql.SQL(
        "SELECT count(*),md5(coalesce(string_agg(row_to_json(t)::text,'' ORDER BY row_to_json(t)::text),'')) FROM ee.{} t"
    ).format(pgsql.Identifier(name))).fetchone() for (name,) in tables}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy", action="store_true")
    parser.add_argument("--browser", action="store_true")
    parser.add_argument("--metadata", action="store_true", help="Apply and test CRUD stage-1 metadata increment only")
    parser.add_argument("--deletion", action="store_true", help="CRUD stage-2 single deletion and active-PRD increment")
    parser.add_argument('--workflow-focused',action='store_true')
    parser.add_argument("--prd-creation-focused", action="store_true", help="Apply local-only PRD intent schema and exercise S2 regression")
    parser.add_argument("--task-focused", action="store_true", help="S4 task schema and tests on disposable localhost port 56431")
    parser.add_argument("--prd-plan-focused", action="store_true", help="Run read-only PRD planning tests in disposable localhost DB")
    parser.add_argument('--workflow-browser', action='store_true')
    parser.add_argument('--preview-only', action='store_true', help='Initialize localhost workflow fixture without running regression suites')
    parser.add_argument('--workflows', action='store_true', help='Apply new workflow increment in disposable database only')
    parser.add_argument('--s5-browser', action='store_true', help='Run the S5 combined local database and browser regression only')
    args = parser.parse_args()
    if args.s5_browser:
        args.task_focused = True
        args.workflow_browser = True
        args.workflows = True
        args.prd_creation_focused = True
    if args.task_focused and not args.s5_browser and (args.legacy or args.browser or args.workflow_browser or args.preview_only or args.workflow_focused or args.prd_creation_focused or args.prd_plan_focused):
        parser.error('--task-focused is an independent local-only fixture')
    if args.task_focused:
        args.prd_creation_focused = True
    if args.preview_only and (not args.workflow_browser or args.legacy or args.browser):
        parser.error('--preview-only requires --workflow-browser and the local workflow fixture')
    if args.workflow_browser or args.workflow_focused or args.prd_plan_focused or args.prd_creation_focused:args.workflows=True
    if args.workflows:
        args.deletion = True
    if args.deletion:
        args.metadata = True
        if args.browser:
            parser.error("Stage 2 is backend-only; preserve the running manual preview")
    if args.legacy and (args.browser or args.metadata):
        parser.error("Run the manual fixture for browser coverage")
    port = 56431 if args.task_focused else 56428 if args.legacy else 56429
    run = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    output = BACK / ("artifacts/deletion-20260928" if args.deletion else "artifacts/metadata-edit-20260928" if args.metadata else "artifacts/writer-rename-20260924") / run
    data = FRONT / "tmp" / ("manual-isolated-" + run) / "data"
    output.mkdir(parents=True)
    data.parent.mkdir(parents=True)
    dbname = ("ee_project_read_" if args.legacy else "ee_manual_") + run.replace("-", "_")
    admin = make_conninfo(host="127.0.0.1", port=port, dbname=dbname, user="postgres", connect_timeout=3, sslmode="disable")
    password = secrets.token_urlsafe(24)  # Disposable credential, never printed or saved in a DSN file.
    reader = make_conninfo(admin, user="ee_project_api_ro")
    writer = make_conninfo(admin, user="ee_project_api_rw", password=password)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PG", "TEST_", "PROJECTS_", "BUSINESS_", "DEMO_", "SECRET_", "AGENT_"))}
    env.update(PYTHONIOENCODING="utf-8", PGCLIENTENCODING="UTF8", APP_ENV="development",
        DEMO_USERNAME="engineer01", DEMO_PASSWORD="change-me-before-demo", SECRET_KEY="isolated-manual-test-secret",
        PROJECTS_DATABASE_URL="", PROJECTS_WRITE_DATABASE_URL="", PROJECTS_CREATE_SUBJECTS="",
        BUSINESS_WRITES_ENABLED="false", AGENT_API_KEY="", AGENT_INVOKE_URL="", COOKIE_SECURE="false")
    if args.task_focused:
        env['TEST_TASK_RECORDS_ENABLED'] = '1'
    report = {"scope": "disposable localhost PG18; no company access", "status": "RUNNING"}
    started = False

    def command(argv, log, environment=None, input_text=None):
        with (output / log).open("ab") as stream:
            return subprocess.run([str(a) for a in argv], cwd=BACK, env=environment or env,
                input=None if input_text is None else input_text.encode("utf8"),
                stdin=subprocess.DEVNULL if input_text is None else None,
                stdout=stream, stderr=subprocess.STDOUT, creationflags=FLAGS, timeout=300).returncode

    psql = [PG / "psql.exe", "-X", "-w", "-h", "127.0.0.1", "-p", str(port), "-U", "postgres", "-d", dbname, "-v", "ON_ERROR_STOP=1"]

    def execute_sql(body):
        if command(psql, "sql.log", input_text=body):
            raise RuntimeError("Local SQL failed; see sql.log")

    def package_command(entry, target, log, extra=()):
        # Same validated stdin pipeline used in the company runbook; no home-dir permission workaround.
        rendered = subprocess.run([sys.executable, str(NEW / "render_sql.py"), entry], cwd=BACK,
            env=env, capture_output=True, creationflags=FLAGS, timeout=30)
        if rendered.returncode:
            raise RuntimeError("SQL bundle rendering failed")
        return command([*psql, *target, *extra, "-f", "-"], log, input_text=rendered.stdout.decode("utf8"))

    try:
        for folder in (BASE, READ, OLD, NEW):
            check_bundle(folder)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))
        if command([PG / "initdb.exe", "-D", data, "-U", "postgres", "-A", "trust", "--encoding=UTF8", "--locale=C"], "init.log"):
            raise RuntimeError("initdb failed")
        hba = data / "pg_hba.conf"
        # Force real password authentication for both old and new writer names.
        hba.write_text("host all ee_project_api_create,ee_project_api_rw 127.0.0.1/32 scram-sha-256\n" + hba.read_text(), encoding="utf8")
        if command([PG / "pg_ctl.exe", "-D", data, "-l", output / "postgres.log", "-o", f"-h 127.0.0.1 -p {port}", "-w", "start"], "start.log"):
            raise RuntimeError("Local PostgreSQL failed to start")
        started = True
        with psycopg.connect(make_conninfo(admin, dbname="postgres"), autocommit=True) as conn:
            system_id = conn.execute("SELECT system_identifier::text FROM pg_control_system()").fetchone()[0]
            if system_id == COMPANY_SYSTEM_ID:
                raise RuntimeError("Company cluster forbidden")
            conn.execute("CREATE ROLE ee_m0_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS")
            conn.execute(pgsql.SQL("CREATE DATABASE {} OWNER ee_m0_owner TEMPLATE template0").format(pgsql.Identifier(dbname)))
        ddl = (BASE / "001_m0_schema.sql").read_text(encoding="utf8")
        helper = (BASE / "server_step.py").read_text(encoding="utf8")
        admin_ddl = re.search(r' admin="""(CREATE SCHEMA ee_admin.*?\n)"""', helper, re.S).group(1)
        digest = hashlib.sha256((BASE / "001_m0_schema.sql").read_bytes()).hexdigest()
        execute_sql("BEGIN;" + admin_ddl + ddl + f"INSERT INTO ee_admin.schema_migrations(version,sha256,applied_by) VALUES('001_m0_metadata_r2_20260921','{digest}','isolated:manual');COMMIT;")
        prefix = "SET LOCAL ee.import_subject='manual:ssh:isolated';SET LOCAL ee.import_display_name='Local test';SET LOCAL ee.reader_subject='engineer01';"
        execute_sql("BEGIN;" + prefix + (BASE / "002_seed.sql").read_text(encoding="utf8") + "COMMIT;")
        execute_sql("BEGIN;" + prefix + (READ / "001_prepare.sql").read_text(encoding="utf8") + (READ / "002_reader_role.sql").read_text(encoding="utf8") + "COMMIT;")
        execute_sql("BEGIN;" + (OLD / "001_creator_role.sql").read_text(encoding="utf8") + (OLD / "002_verify_creator.sql").read_text(encoding="utf8") + "COMMIT;")
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute("SET password_encryption='scram-sha-256'")
            conn.execute(pgsql.SQL("ALTER ROLE ee_project_api_create PASSWORD {}").format(pgsql.Literal(password)))
            old_oid = conn.execute("SELECT oid FROM pg_roles WHERE rolname='ee_project_api_create'").fetchone()[0]
            before = business_snapshot(conn)
        with psycopg.connect(make_conninfo(writer, user="ee_project_api_create")) as conn:
            assert conn.execute("SELECT current_user").fetchone()[0] == "ee_project_api_create"
        target = ["-v", "expected_database=" + dbname, "-v", "expected_system_id=" + system_id]
        if package_command("000_inspect.sql", target, "inspect-before.log"):
            raise RuntimeError("Local inspection failed")
        if args.preview_only:
            report['writer_migration_regression']='NOT_RUN_LOCAL_PREVIEW'
        else:
            migration_env = {**env, "TEST_MIGRATION_ADMIN_DSN": admin, "TEST_MIGRATION_WRITER_DSN": make_conninfo(writer, user="ee_project_api_create")}
            rc = command([sys.executable, "-m", "pytest", "-q", "tests/test_writer_migration_postgres.py"], "migration-tests.log", migration_env)
            print((output / "migration-tests.log").read_text(encoding="utf8"), flush=True)
            if rc:
                raise RuntimeError("Migration safety regression failed")
        if package_command("apply.sql", target, "migration.log"):
            raise RuntimeError("Atomic migration failed")
        if package_command("verify.sql", target, "verify.log"):
            raise RuntimeError("Post-migration verification failed")
        with psycopg.connect(admin, autocommit=True) as conn:
            assert conn.execute("SELECT oid FROM pg_roles WHERE rolname='ee_project_api_rw'").fetchone()[0] == old_oid
            assert conn.execute("SELECT count(*) FROM pg_roles WHERE rolname='ee_project_api_create'").fetchone()[0] == 0
            assert business_snapshot(conn) == before
        with psycopg.connect(writer) as conn:
            assert conn.execute("SELECT current_user").fetchone()[0] == "ee_project_api_rw"
        for invalid in (make_conninfo(writer, password="deliberately-incorrect"), make_conninfo(writer, user="ee_project_api_create")):
            try:
                with psycopg.connect(invalid):
                    raise RuntimeError("Invalid credentials unexpectedly accepted")
            except psycopg.OperationalError:
                pass
        report.update(migration="PASS", password_authentication="SCRAM before/after; wrong-password/old-name rejected", business_rows_unchanged_by_migration=True)
        # A repeated apply must fail without side effects; verify remains repeatable.
        if not package_command("apply.sql", target, "repeat-rejected.log"):
            raise RuntimeError("Repeated migration unexpectedly accepted")
        if package_command("verify.sql", target, "verify-repeat.log"):
            raise RuntimeError("Verification after rejected repeat failed")
        if not args.legacy:
            for _ in range(2):
                if package_command("003_bind_project.sql", target, "binding.log", ["-v", "project_id=10d4c8a0-ff3a-56c3-abf6-18dc3b531e62", "-v", "subject=engineer01"]):
                    raise RuntimeError("Project author binding failed")
            with psycopg.connect(admin) as conn:
                assert conn.execute("SELECT count(*) FROM ee.security_audit_events WHERE event_type='manual_work.grant'").fetchone()[0] == 1
        if args.metadata:
            meta = FRONT / "docs/current/database/metadata-edit-20260928"
            check_bundle(meta)
            with psycopg.connect(admin) as conn: metadata_before = business_snapshot(conn)
            for entry in ("inspect.sql", "apply.sql", "verify.sql", "rollback.sql", "apply.sql", "verify.sql"):
                if command([*psql, *target, "-f", meta / entry], "metadata-migration.log"):
                    raise RuntimeError("Metadata increment failed: " + entry)
            if not command([*psql, *target, "-f", meta / "apply.sql"], "metadata-repeat-rejected.log"):
                raise RuntimeError("Repeated metadata apply unexpectedly accepted")
            with psycopg.connect(admin) as conn:
                assert business_snapshot(conn) == metadata_before
            env.update(TEST_METADATA_ENABLED="1", EE_METADATA_TEST="1")
            report["metadata_increment"] = "apply/verify/rollback/reapply/repeat guard PASS; business rows unchanged"
        if args.deletion:
            deletion = FRONT / "docs/current/database/deletion-20260928"
            check_bundle(deletion)
            with psycopg.connect(admin) as conn: deletion_before = business_snapshot(conn)
            # Exercise fail-closed target/ACL/partial-install and transactional rollback guards.
            def deletion_state():
                with psycopg.connect(admin) as conn:
                    return (
                        business_snapshot(conn),
                        conn.execute("SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname='ee' ORDER BY tablename,indexname").fetchall(),
                        conn.execute("SELECT oid::regclass::text,relacl::text FROM pg_class WHERE relnamespace='ee'::regnamespace ORDER BY oid").fetchall(),
                        conn.execute("SELECT attrelid::regclass::text,attnum,attacl::text FROM pg_attribute WHERE attrelid IN (SELECT oid FROM pg_class WHERE relnamespace='ee'::regnamespace) ORDER BY attrelid,attnum").fetchall(),
                        conn.execute("SELECT pg_get_functiondef('ee.accept_manual_revision(uuid,uuid,uuid,uuid,text)'::regprocedure)").fetchone(),
                        conn.execute("SELECT code,resource_kind,action,enabled FROM ee.permission_definitions ORDER BY code").fetchall(),
                        conn.execute("SELECT role_id,permission_code FROM ee.role_permissions ORDER BY role_id,permission_code").fetchall(),
                    )
            rejected = []
            cases = [
                ("wrong-database", None, None, ["-v", "expected_database=not_the_target"]),
                ("wrong-cluster", None, None, ["-v", "expected_system_id=0"]),
                ("reader-write", "GRANT UPDATE(archived_at) ON ee.artifacts TO ee_project_api_ro", "REVOKE UPDATE(archived_at) ON ee.artifacts FROM ee_project_api_ro", []),
                ("broad-writer-delete", "GRANT DELETE ON ee.artifacts TO ee_project_api_rw", "REVOKE DELETE ON ee.artifacts FROM ee_project_api_rw", []),
                ("partial-actions", "INSERT INTO ee.permission_definitions(code,display_name,resource_kind,action,allowed_scopes) VALUES('project.archive','test','project','archive',ARRAY['project'])", "DELETE FROM ee.permission_definitions WHERE code='project.archive'", []),
                ("mid-apply-failure", "ALTER TABLE ee.permission_definitions ADD CONSTRAINT deletion_fail_mid_apply CHECK(code NOT LIKE '%.archive') NOT VALID", "ALTER TABLE ee.permission_definitions DROP CONSTRAINT deletion_fail_mid_apply", []),
            ]
            for name, setup, cleanup, extra in cases:
                if setup: execute_sql(setup)
                try:
                    before_rejection = deletion_state()
                    if not command([*psql, *target, *extra, "-f", deletion / "apply.sql"], "deletion-negative-" + name + ".log"):
                        raise RuntimeError("Unsafe deletion migration accepted: " + name)
                    assert deletion_state() == before_rejection, name
                    rejected.append(name)
                finally:
                    if cleanup: execute_sql(cleanup)
            report["deletion_migration_negative_guards"] = rejected
            for entry in ("inspect.sql", "apply.sql", "verify.sql", "rollback.sql", "apply.sql", "verify.sql"):
                if command([*psql, *target, "-f", deletion / entry], "deletion-migration.log"):
                    raise RuntimeError("Deletion increment failed: " + entry)
            if not command([*psql, *target, "-f", deletion / "apply.sql"], "deletion-repeat-rejected.log"):
                raise RuntimeError("Repeated deletion apply unexpectedly accepted")
            with psycopg.connect(admin) as conn:
                assert business_snapshot(conn) == deletion_before
            env["TEST_DELETION_ENABLED"] = "1"
            report["deletion_increment"] = "apply/verify/rollback/reapply/repeat guard PASS; business rows unchanged"
        if args.workflows:
            permissions = FRONT / 'docs/current/database/permission-admin-20260929'
            check_bundle(permissions)
            if command([*psql,*target,'-f',permissions/'apply.sql'],'workflow-auth-migration.log'):
                raise RuntimeError('Workflow auth baseline failed')
            from pwdlib import PasswordHash
            with psycopg.connect(admin) as conn:
                conn.execute('INSERT INTO ee.app_credentials(user_id,login_name,password_hash) SELECT id,%s,%s FROM ee.app_users WHERE external_subject=%s',('engineer01',PasswordHash.recommended().hash(env['DEMO_PASSWORD']),'engineer01'))
            workflow = FRONT / 'docs/current/database/agent-workflows-r1'
            from run_workflow_migration import verify_migration
            report['workflow_migration_safety'] = verify_migration(command, psql, target, workflow, admin, business_snapshot, expected_port=port)
            report['workflow_increment'] = 'applied in disposable local PostgreSQL only'
        if args.prd_creation_focused:
            intent_bundle = FRONT / 'docs/current/database/prd-creation-intents-20261006'
            check_bundle(intent_bundle)
            if args.task_focused:
                # Frozen migration guards 56429; adapt only a verified, disposable copy.
                sql_root = output / 'local-sql'
                copied = sql_root / intent_bundle.name
                shutil.copytree(intent_bundle, copied)
                baseline = sql_root / 'agent-workflows-r1'
                baseline.mkdir()
                shutil.copy2(workflow / 'combined-baseline.sql', baseline / 'combined-baseline.sql')
                original = (intent_bundle / 'target.sql').read_bytes()
                old, new = b"current_setting('port') <> '56429'", b"current_setting('port') <> '56431'"
                if original.count(old) != 1:
                    raise RuntimeError('Frozen PRD intent port guard changed; refuse local adaptation')
                (copied / 'target.sql').write_bytes(original.replace(old, new))
                # The final combined verifier must include only the seven S4
                # task UPDATE columns, without weakening the frozen baseline.
                verification = (copied / 'combined-baseline.sql').read_text(encoding='utf-8')
                needle = "'lease_token','lease_until','error_code','updated_at'));"
                if verification.count(needle) != 1:
                    raise RuntimeError('Frozen intent verifier changed; refuse adaptation')
                verification = verification.replace(needle,
                    "'lease_token','lease_until','error_code','updated_at'))\n"
                    "    OR (t.relname='chat_tasks' AND a.attname IN ('stage','status','function_id','result_count','result_refs','receipt_id','updated_at'));", 1)
                (copied / 'combined-baseline.sql').write_text(verification, encoding='utf-8')
                intent_bundle = copied
                report['intent_sql'] = 'verified frozen source; local 56431 guard and exact 7 task-column verifier copy only; not a release migration'
            with psycopg.connect(admin) as conn:
                intents_before = business_snapshot(conn)
            for name,extra in [('database',['-v','expected_database=wrong_intent_database']),
                               ('cluster',['-v','expected_system_id=0'])]:
                if not command([*psql,*target,*extra,'-f',intent_bundle/'apply.sql'],'prd-intent-negative-'+name+'.log'):
                    raise RuntimeError('Intent migration target guard failed: '+name)
                with psycopg.connect(admin) as conn:
                    assert business_snapshot(conn) == intents_before
                    assert conn.execute("SELECT to_regclass('ee.prd_creation_intents')").fetchone()[0] is None
            for entry in ('apply.sql','verify.sql','rollback.sql','apply.sql','verify.sql'):
                if command([*psql,*target,'-f',intent_bundle/entry],'prd-intent-migration.log'):
                    raise RuntimeError('Local PRD intent migration failed: '+entry)
            if not command([*psql,*target,'-f',intent_bundle/'apply.sql'],'prd-intent-repeat-rejected.log'):
                raise RuntimeError('Repeated PRD intent apply unexpectedly accepted')
            with psycopg.connect(admin) as conn:
                after = business_snapshot(conn)
                assert after.pop('prd_creation_intents')[0] == 0
                assert after == intents_before
            env['TEST_PRD_CREATION_ENABLED'] = '1'
            report['prd_intent_increment'] = 'local-only apply/verify/empty rollback/reapply/repeat rejection PASS; existing business rows unchanged'
        if args.task_focused:
            task_bundle = FRONT / 'docs/current/database/task-records-20261007'
            check_bundle(task_bundle)
            task_target = [*target, '-v', 'expected_port=56431']
            with psycopg.connect(admin) as conn:
                task_before = business_snapshot(conn)
            for name, extra in (('database', ['-v', 'expected_database=wrong_task_database']),
                                ('cluster', ['-v', 'expected_system_id=0']),
                                ('port', ['-v', 'expected_port=56429'])):
                if not command([*psql, *task_target, *extra, '-f', task_bundle / 'apply.sql'], 'task-reject-' + name + '.log'):
                    raise RuntimeError('Task migration accepted wrong ' + name)
                with psycopg.connect(admin) as conn:
                    assert business_snapshot(conn) == task_before
                    assert conn.execute("SELECT to_regclass('ee.chat_tasks')").fetchone()[0] is None
            for entry in ('apply.sql', 'verify.sql', 'rollback.sql', 'apply.sql', 'verify.sql'):
                if command([*psql, *task_target, '-f', task_bundle / entry], 'task-migration.log'):
                    raise RuntimeError('Task migration failed: ' + entry)
            if not command([*psql, *task_target, '-f', task_bundle / 'apply.sql'], 'task-repeat-rejected.log'):
                raise RuntimeError('Repeated task migration unexpectedly accepted')
            with psycopg.connect(admin) as conn:
                after = business_snapshot(conn)
                assert after.pop('chat_tasks')[0] == 0
                assert after == task_before
            report['task_increment'] = '56431-only apply/verify/empty rollback/reapply/repeat rejection PASS; prior rows unchanged'
        prefix = "TEST_PROJECTS_" if args.legacy else "TEST_MANUAL_"
        test_env = {**env, prefix + "ADMIN_DSN": admin, prefix + "READER_DSN": reader, prefix + "WRITER_DSN": writer}
        if args.legacy:
            tests = ["tests/test_projects_postgres.py", "tests/test_project_creation_postgres.py"]
        elif args.deletion:
            # Run the complete live manual/deletion suite against this disposable PG,
            # including the stage-3/4 batch tests; never use the company fixture.
            tests = ["tests/test_manual_work_postgres.py", "tests/test_metadata_edit_postgres.py", "tests/test_deletion_postgres.py", "tests/test_bulk_deletion_postgres.py"]
        else:
            tests = []
        if args.workflows:
            tests += ['tests/test_workflows_postgres.py']
        if args.workflow_focused:tests=['tests/test_workflows_protocol.py','tests/test_workflows_postgres.py']
        if args.prd_plan_focused:
            tests += ["tests/test_prd_creation_plan.py", "tests/test_prd_creation_plan_postgres.py", "tests/test_workflows_protocol.py"]
        if args.prd_creation_focused:
            tests += ["tests/test_prd_creation_runner.py", "tests/test_prd_creation.py", "tests/test_prd_creation_postgres.py", "tests/test_prd_creation_plan.py",
                      "tests/test_prd_creation_plan_postgres.py", "tests/test_workflows_protocol.py"]
        if args.task_focused:
            if not args.s5_browser:
                tests = []
            else:
                tests += ['tests/test_generation_append_postgres.py', 'tests/test_auto_chat_postgres.py']
            tests += ['tests/test_chat_tasks_postgres.py', 'tests/test_chat_tasks.py']
            tests = list(dict.fromkeys(tests))
        if args.preview_only:
            report.update(suite='local-preview',pytest_exit=None,regression='NOT_RUN_LOCAL_PREVIEW')
            print('LOCAL_PREVIEW_MODE: regression suites are separate; no test PASS is claimed.',flush=True)
        else:
            rc = command([sys.executable, "-m", "pytest", "-q", *tests], "pytest.log", test_env)
            pytest_output = (output / "pytest.log").read_text(encoding="utf8")
            # Windows PowerShell may expose a legacy GBK stdout; keep Unicode test
            # evidence printable instead of turning a passing suite into a harness failure.
            stdout_encoding = sys.stdout.encoding or "utf-8"
            sys.stdout.write(pytest_output.encode(stdout_encoding, errors="backslashreplace").decode(stdout_encoding))
            sys.stdout.flush()
            report.update(suite="read-project-create-with-migrated-writer" if args.legacy else "manual-work", pytest_exit=rc)
            if rc:
                raise RuntimeError("Application regression failed")
        if args.prd_creation_focused and not args.preview_only:
            with psycopg.connect(admin) as conn:
                before_rollback = business_snapshot(conn)
            if not command([*psql,*target,'-f',intent_bundle/'rollback.sql'],'prd-intent-data-rollback-rejected.log'):
                raise RuntimeError('Nonempty PRD intent rollback unexpectedly accepted')
            with psycopg.connect(admin) as conn:
                assert business_snapshot(conn) == before_rollback
            report['prd_intent_nonempty_rollback'] = 'refused without changing existing data'
        if args.task_focused and not args.preview_only:
            with psycopg.connect(admin) as conn:
                before_rollback = business_snapshot(conn)
                assert before_rollback['chat_tasks'][0] > 0
            if not command([*psql, *task_target, '-f', task_bundle / 'rollback.sql'], 'task-data-rollback-rejected.log'):
                raise RuntimeError('Nonempty task rollback unexpectedly accepted')
            if command([*psql, *task_target, '-f', task_bundle / 'verify.sql'], 'task-final-verify.log'):
                raise RuntimeError('Task schema invalid after rejected rollback')
            with psycopg.connect(admin) as conn:
                assert business_snapshot(conn) == before_rollback
            report['task_nonempty_rollback'] = 'refused; task and prior business rows unchanged'
        if args.workflow_browser:
            from run_workflow_browser import run_workflow_browser
            run_workflow_browser(BACK,FRONT,output,env,admin,reader,writer)
            report['workflow_browser']='NOT_RUN_LOCAL_PREVIEW' if args.preview_only else 'PASS'
            if not args.preview_only:
                from run_workflow_migration import verify_data_rollback
                verify_data_rollback(command, psql, target, workflow, admin, business_snapshot,
                                     verification_package=intent_bundle if args.prd_creation_focused else None)
                report['workflow_data_rollback_refused_without_changes'] = True
        if args.browser:
            from run_manual_browser import run_browser
            run_browser(BACK, FRONT, output, env, reader, writer)
            report["browser"] = "PASS"
        if args.deletion and not (args.workflow_focused or args.task_focused):
            from run_deletion_smoke import run_http_smoke
            run_http_smoke(BACK, output, env, admin, reader, writer)
            report["deletion_real_http_smoke"] = "PASS; health/docs/redoc/login/me/logout, dependency block, new PRD identity, audit replay"
            with psycopg.connect(admin) as conn: after_work = business_snapshot(conn)
            if not command([*psql, *target, "-f", deletion / "rollback.sql"], "deletion-destructive-rollback-rejected.log"):
                raise RuntimeError("Rollback with multiple PRD histories unexpectedly succeeded")
            # Frozen deletion assertions predate account and workflow grants.
            # Verify the exact combined baseline, never weaken the old package.
            final_package = intent_bundle if args.prd_creation_focused else workflow if args.workflows else deletion
            if command([*psql, *target, '-f', final_package / 'verify.sql'], 'deletion-final-verify.log'):
                raise RuntimeError("Deletion verification failed after rejected rollback")
            with psycopg.connect(admin) as conn:
                assert business_snapshot(conn) == after_work
            report["deletion_unsafe_rollback_rejected_without_data_changes"] = True
        if args.metadata and not args.deletion:
            with psycopg.connect(admin) as conn: after_work = business_snapshot(conn)
            if command([*psql, *target, "-f", meta / "rollback.sql"], "metadata-final-rollback.log"):
                raise RuntimeError("Metadata final rollback failed")
            with psycopg.connect(admin) as conn:
                assert business_snapshot(conn) == after_work
            report["metadata_rollback_keeps_data"] = True
        report["status"] = "PASS"
    except Exception as exc:
        report.update(status="FAIL", error=str(exc))
        raise
    finally:
        if started:
            stopped = command([PG / "pg_ctl.exe", "-D", data, "-m", "fast", "-w", "stop"], "stop.log")
            report["local_postgres_stopped"] = stopped == 0
            if stopped:
                report["status"] = "FAIL"
        (output / "verification.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf8")
        print("Evidence: " + str(output), flush=True)
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
