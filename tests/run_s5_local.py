"""Local-only S5 combined PostgreSQL and browser acceptance harness."""
import importlib.util
import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["S5_COMBINED"] = "1"
spec = importlib.util.spec_from_file_location("run_manual_postgres", ROOT / "tests" / "run_manual_postgres.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
sys.argv = [str(ROOT / "tests" / "run_manual_postgres.py"), "--s5-browser"]
raise SystemExit(runner.main())
