"""No database access: validate the permission-admin SQL package renderer."""
import hashlib
import importlib.util
from pathlib import Path

import pytest

BUNDLE = Path(__file__).resolve().parents[2] / "TS-frontend/docs/current/database/permission-admin-20260929"
SPEC = importlib.util.spec_from_file_location("permission_sql_renderer", BUNDLE / "render_sql.py")
RENDERER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(RENDERER)


@pytest.mark.parametrize("entry", RENDERER.ENTRIES)
def test_permission_bundle_renders_complete_sql(entry):
    rendered = RENDERER.render_entry(entry)
    assert "\\ir " not in rendered
    assert "expected_database" in rendered and "expected_system_id" in rendered
    assert "BEGIN" in rendered and "COMMIT;" in rendered
    if entry == "apply.sql":
        assert "CREATE TABLE ee.app_credentials" in rendered
        assert "password_hash" in rendered
        assert "function_editor" in rendered


def test_changed_permission_sql_is_rejected_before_output(tmp_path):
    files = {name: (BUNDLE / name).read_bytes() for name in ("apply.sql", "target.sql", "assertions.sql")}
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    (tmp_path / "SHA256SUMS").write_text(
        "".join(hashlib.sha256(data).hexdigest() + "  " + name + "\n" for name, data in files.items()),
        encoding="ascii",
    )
    (tmp_path / "apply.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum"):
        RENDERER.render_entry("apply.sql", tmp_path)


def test_unverified_include_is_rejected(tmp_path):
    (tmp_path / "apply.sql").write_text("\\ir target.sql", encoding="utf-8")
    (tmp_path / "SHA256SUMS").write_text(hashlib.sha256((tmp_path / "apply.sql").read_bytes()).hexdigest() + "  apply.sql\n", encoding="ascii")
    with pytest.raises(ValueError, match="unverified"):
        RENDERER.render_entry("apply.sql", tmp_path)
