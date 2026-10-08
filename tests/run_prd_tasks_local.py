"""Local S5/S6 preview and regression; never company databases or Agent URLs."""
import importlib.util
import os
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
sys.path.insert(0, str(root / 'tests'))
serve = '--serve' in sys.argv
smoke = '--smoke' in sys.argv
if smoke and not serve:
    raise SystemExit('--smoke requires --serve')
if set(sys.argv[1:]) - {'--serve', '--smoke'}:
    raise SystemExit('Supported options: --serve [--smoke]')
os.environ['S5_KEEP_PREVIEW'] = '1' if serve else '0'
os.environ['S5_PREVIEW_SMOKE'] = '1' if smoke else '0'
os.environ['PYTHONIOENCODING'] = 'utf-8'
spec = importlib.util.spec_from_file_location('local_runner', root / 'tests/run_manual_postgres.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
sys.argv = [str(root / 'tests/run_manual_postgres.py'), '--s5-browser'] + (['--preview-only'] if serve else [])
sys.exit(runner.main())
