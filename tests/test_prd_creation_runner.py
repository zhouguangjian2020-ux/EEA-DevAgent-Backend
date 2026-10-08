"""The newer combined baseline must be verified without weakening frozen SQL."""
from pathlib import Path
from tests import run_workflow_migration as runner

class Db:
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def execute(self, *args): return self
    def fetchone(self): return (1,)

def test_new_intent_baseline_verifies_after_old_workflow_rollback_rejection(monkeypatch):
    calls=[]
    monkeypatch.setattr(runner.psycopg, 'connect', lambda _: Db())
    def command(args, name):
        calls.append((args, name))
        return 1 if name == 'workflow-data-rollback-rejected.log' else 0
    runner.verify_data_rollback(command, 'psql', ['target'], Path('old'), 'local', lambda _: {'rows': 1}, verification_package=Path('intent'))
    assert calls[0][0][-1] == Path('old/rollback-empty-only.sql')
    assert calls[1][0][-1] == Path('intent/verify.sql')

def test_original_workflow_baseline_keeps_its_verifier(monkeypatch):
    calls=[]
    monkeypatch.setattr(runner.psycopg, 'connect', lambda _: Db())
    def command(args, name):
        calls.append(args)
        return 1 if 'rejected' in name else 0
    runner.verify_data_rollback(command, 'psql', [], Path('old'), 'local', lambda _: {})
    assert calls[1][-1] == Path('old/verify.sql')
