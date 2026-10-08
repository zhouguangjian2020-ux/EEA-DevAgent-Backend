# Reuse the audited localhost-only disposable PostgreSQL harness.
import importlib.util,subprocess,sys,os
from pathlib import Path
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root));sys.path.insert(0,str(root/'tests'))
spec=importlib.util.spec_from_file_location('local_runner',root/'tests/run_manual_postgres.py')
runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
browser_requested='--browser' in sys.argv or '--serve' in sys.argv
serve_requested='--serve' in sys.argv
os.environ['UC_CHAT_KEEP_PREVIEW']='1' if serve_requested else '0'
os.environ['UC_CHAT_SKIP_BROWSER']='1' if serve_requested else '0'
if browser_requested:
    import run_workflow_browser
    from run_uc_fr_chat_preview import run_preview
    run_workflow_browser.run_workflow_browser=run_preview
original=subprocess.run
def command(argv,*args,**kwargs):
    cmd=[str(x) for x in argv]
    if '-m' in cmd and 'pytest' in cmd and 'tests/test_workflows_postgres.py' in cmd:
        cmd+=['tests/test_generation_append_postgres.py','tests/test_auto_chat_postgres.py']
    return original(cmd,*args,**kwargs)
subprocess.run=command
sys.argv=[str(root/'tests/run_manual_postgres.py'),'--workflow-focused'] + (['--workflow-browser'] if browser_requested else []) + (['--preview-only'] if serve_requested else [])
try:rc=runner.main()
finally:subprocess.run=original
sys.exit(rc)
