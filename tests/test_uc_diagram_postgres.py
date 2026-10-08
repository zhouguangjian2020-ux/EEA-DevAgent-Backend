"""Real SQL tests on the disposable localhost PG18 fixture, never company DSNs."""
import asyncio
from uuid import UUID, uuid4

import pytest

from app.services.uc_diagram import read_accepted_ucs
from app.services.uc_generation import read_confirmed_prd
from app.services.projects import ProjectReadError
from tests.test_manual_work_postgres import db, client, PROJECT, new_function, create, accept, post


def make_uc(client, fid, prd, name="记录事件"):
    uc = create(client, fid, "UC", name=name, prd_revision_id=prd["revision_id"])
    return post(client, f'/artifacts/{uc["artifact_id"]}/revisions', {
        "mutation_id": str(uuid4()), "base_revision_id": uc["revision_id"],
        "content": {"kind": "UC", "name": name, "description": "已确认描述", "main_flow": "完整主流程\n末尾🚗",
                    "prd_revision_id": prd["revision_id"], "actors": "驾驶员；设备",
                    "branches": ["分支A", "分支B", "", ""], "exceptions": ["", "机械故障处理", "", "", "电源恢复"]},
    })


def test_uc_diagram_real_sql_uses_accepted_revisions_and_function_scope(client, db):
    fid = new_function(client)
    other_fid = new_function(client)
    prd = create(client, fid, "PRD", name="UC接口隔离验证")
    accept(client, prd)
    one = make_uc(client, fid, prd, "录像")
    two = make_uc(client, fid, prd, "回放")
    draft = make_uc(client, fid, prd, "未确认的草稿")
    accept(client, one); accept(client, two)
    other_prd = create(client, other_fid, "PRD", name="其他功能")
    accept(client, other_prd)
    other = make_uc(client, other_fid, other_prd, "不可混入")
    accept(client, other)
    source = read_accepted_ucs("engineer01", project_id=UUID(PROJECT), function_id=UUID(fid))
    assert {str(uc.uc_id) for uc in source.ucs} == {one["artifact_id"], two["artifact_id"]}
    assert all(uc.branches == ["分支A", "分支B"] for uc in source.ucs)
    assert all(uc.main_flow.endswith("末尾🚗") for uc in source.ucs)
    # Updating an accepted object's working draft must NOT change its accepted graph source.
    newer = post(client, f'/artifacts/{one["artifact_id"]}/revisions', {
        "mutation_id": str(uuid4()), "base_revision_id": one["revision_id"],
        "content": {"kind": "UC", "name": "草稿新名称", "description": "草稿新描述",
                    "main_flow": "草稿流程", "prd_revision_id": prd["revision_id"]},
    })
    same = read_accepted_ucs("engineer01", project_id=UUID(PROJECT), function_id=UUID(fid))
    assert same.source_version == source.source_version
    assert "草稿新名称" not in [uc.name for uc in same.ucs]
    accept(client, newer)
    changed = read_accepted_ucs("engineer01", project_id=UUID(PROJECT), function_id=UUID(fid))
    assert changed.source_version != source.source_version
    assert "草稿新名称" in [uc.name for uc in changed.ucs]
    with pytest.raises(ProjectReadError) as error:
        read_accepted_ucs("not-a-member", project_id=UUID(PROJECT), function_id=UUID(fid))
    assert error.value.status == 404
    with pytest.raises(ProjectReadError) as error:
        read_accepted_ucs("engineer01", project_id=UUID(PROJECT), function_id=uuid4())
    assert error.value.status == 404


def test_prd_real_sql_preserves_full_unicode_body_and_current_acceptance(client, db):
    fid = new_function(client)
    prd = create(client, fid, "PRD", name="完整正文验证")
    body = "# PRD\n" + ("中文输入🚗\n" * 10000) + "END-完整正文\n  "
    saved = post(client, f'/artifacts/{prd["artifact_id"]}/revisions', {
        "mutation_id": str(uuid4()), "base_revision_id": prd["revision_id"],
        "content": {"kind": "PRD", "markdown_body": body},
    })
    accept(client, saved)
    # Existing manual save strips outer whitespace; compare the immutable database body.
    stored = db.execute("SELECT markdown_body FROM ee.prd_versions WHERE revision_id=%s", (saved["revision_id"],)).fetchone()[0]
    read = read_confirmed_prd("engineer01", project_id=UUID(PROJECT), function_id=UUID(fid),
        prd_id=UUID(saved["artifact_id"]), revision_id=UUID(saved["revision_id"]))
    assert read.markdown_body == stored
    assert read.markdown_body.rstrip().endswith("END-完整正文")
    assert len(read.markdown_body) > 50000



