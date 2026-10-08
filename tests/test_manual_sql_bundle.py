"""No database access: validate the user-operated SQL rendering/pipeline helper."""
import hashlib
import importlib.util
from pathlib import Path

import pytest

BUNDLE = Path(__file__).resolve().parents[2] / "TS-frontend/docs/current/database/manual-work-20260923"
SPEC = importlib.util.spec_from_file_location("manual_sql_renderer", BUNDLE / "render_sql.py")
RENDERER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RENDERER)


@pytest.mark.parametrize("entry", RENDERER.ENTRIES)
def test_real_bundle_renders_complete_verified_sql(entry):
    result = RENDERER.render_entry(entry)
    assert "\\ir " not in result
    assert "BEGIN" in result and "COMMIT;" in result
    assert "expected_system_id" in result and "expected_database" in result
    if entry == "apply.sql":
        assert result.index("BEGIN;") < result.index("RENAME TO") < result.index("COMMIT;")
        assert "Reader verified:" in result and "Old writer missing" in result


def fixture_bundle(root, files):
    for name, data in files.items():
        (root/name).write_text(data,encoding="utf8")
    (root/"SHA256SUMS").write_text("".join(hashlib.sha256((root/name).read_bytes()).hexdigest()+"  "+name+"\n" for name in files),encoding="ascii")


def test_changed_sql_is_rejected_before_output(tmp_path):
    fixture_bundle(tmp_path,{"apply.sql":"BEGIN; COMMIT;"})
    (tmp_path/"apply.sql").write_text("SELECT 1;",encoding="utf8")
    with pytest.raises(ValueError,match="checksum"):
        RENDERER.render_entry("apply.sql",tmp_path)


def test_unverified_include_is_rejected(tmp_path):
    fixture_bundle(tmp_path,{"apply.sql":"\\ir missing.sql"})
    with pytest.raises(ValueError,match="unverified"):
        RENDERER.render_entry("apply.sql",tmp_path)


def test_include_cycle_is_rejected(tmp_path):
    fixture_bundle(tmp_path,{"apply.sql":"\\ir apply.sql"})
    with pytest.raises(ValueError,match="cyclic"):
        RENDERER.render_entry("apply.sql",tmp_path)


def test_path_traversal_is_rejected(tmp_path):
    (tmp_path/"SHA256SUMS").write_text("0"*64+"  ../outside.sql\n",encoding="ascii")
    with pytest.raises(ValueError,match="manifest path"):
        RENDERER.render_entry("apply.sql",tmp_path)
