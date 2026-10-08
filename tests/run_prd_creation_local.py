"""S3 localhost-only browser harness; uses a fresh disposable intent-enabled DB."""
import importlib.util
import os
from pathlib import Path
import sys
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root));sys.path.insert(0,str(root/'tests'))
import run_workflow_browser
from run_prd_creation_preview import run_preview
run_workflow_browser.run_workflow_browser=run_preview
serve='--serve' in sys.argv
os.environ['UC_CHAT_KEEP_PREVIEW']='1' if serve else '0'
os.environ['UC_CHAT_SKIP_BROWSER']='1' if serve else '0'
spec=importlib.util.spec_from_file_location('local_runner',root/'tests/run_manual_postgres.py')
runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
sys.argv=[str(root/'tests/run_manual_postgres.py'),'--prd-creation-focused','--workflow-browser'] + (['--preview-only'] if serve else [])
sys.exit(runner.main())
