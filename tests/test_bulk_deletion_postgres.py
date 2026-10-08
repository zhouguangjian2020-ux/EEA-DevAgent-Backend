"""Real PostgreSQL batch atomicity, identity, dependency and replay verification."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.core.security import create_session_token
from app.services import bulk_deletion, manual_work
from tests.test_manual_work_postgres import db, client, PROJECT
from tests.test_deletion_postgres import resource, chain, create, post, save_fr, accept, allocation, deletion_increment_required
from tests.test_deletion import HEADERS


def selection(f, *artifacts):
    return [{"id": f, "kind": "function"}] + [{"id": a["artifact_id"], "kind": kind} for kind, a in artifacts]


def check(c, p, items, status=200):
    r = c.post(f"/v1/projects/{p}/deletion-batch/impact", headers=HEADERS, json={"items": items})
    assert r.status_code == status, r.text
    return r.json()


def request(impact, mid=None):
    return {"mutation_id": mid or str(uuid4()), "items": [{"id": i["target"]["id"], "kind": i["target"]["kind"], "base_lock_version": i["target"]["lock_version"]} for i in impact["items"]]}


def submit(c, p, payload, status=200):
    r = c.post(f"/v1/projects/{p}/deletion-batch", headers=HEADERS, json=payload)
    assert r.status_code == status, r.text
    return r.json()


def assert_active(db, ids):
    assert db.execute("SELECT count(*) FROM ee.artifacts WHERE id=ANY(%s::uuid[]) AND archived_at IS NULL", (ids,)).fetchone()[0] == len(ids)


def test_entire_chain_transaction_order_and_replay(client, db, resource):
    p, f = resource; pid = p["id"]
    prd, uc, fr = chain(client, pid, f)
    items = selection(f, ("PRD", prd), ("UC", uc), ("FR", fr))
    preview = check(client, pid, items)
    assert not preview["blocked"] and preview["can_delete"] and preview["total"] == 4
    payload = request(preview)
    result = submit(client, pid, payload)
    assert [i["kind"] for i in result["items"]] == ["FR", "UC", "PRD", "function"]
    assert submit(client, pid, {**payload, "items": list(reversed(payload["items"]))}) == result
    assert db.execute("SELECT count(*) FROM ee.security_audit_events WHERE id=%s", (payload["mutation_id"],)).fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM ee.artifacts WHERE function_id=%s AND archived_at IS NULL", (f,)).fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM ee.prd_versions WHERE prd_id=%s", (prd["artifact_id"],)).fetchone()[0] == 1
    submit(client, pid, {**payload, "mutation_id": str(uuid4())}, 404)
    submit(client, pid, {**payload, "items": payload["items"][:-1]}, 409)
    db.execute("UPDATE ee.access_bindings SET role_id=(SELECT id FROM ee.access_roles WHERE code='project_api_reader') WHERE project_id=%s", (pid,))
    submit(client, pid, payload, 403)


def test_external_dependency_blocks_entire_batch_no_implicit_cascade(client, db, resource):
    p, f = resource; pid = p["id"]; prd, uc, fr = chain(client, pid, f)
    preview = check(client, pid, selection(f, ("PRD", prd), ("UC", uc)))
    assert preview["blocked"] and any(b["id"] == fr["artifact_id"] for i in preview["items"] for b in i["blockers"])
    submit(client, pid, request(preview), 409)
    assert_active(db, [prd["artifact_id"], uc["artifact_id"], fr["artifact_id"]])


def test_allocation_and_fr_batch_keeps_directory_and_unselected_chain(client, db):
    f = post(client, f'/{PROJECT}/functions', {"creation_id": str(uuid4()), "name": "多目标批量"})["function_id"]
    prd, uc, fr = chain(client, PROJECT, f)
    fr2 = create(client, PROJECT, f, "FR", description="空分配需求")
    a1, _, _ = allocation(db, f, fr, True); a2, _, _ = allocation(db, f, fr2, False)
    items = [{"id": fr["artifact_id"], "kind": "FR"}, {"id": a1, "kind": "FR_ALLOCATION"}, {"id": fr2["artifact_id"], "kind": "FR"}]
    info = check(client, PROJECT, items); assert info["blocked"]
    assert info["items"][2]["blockers"][0]["id"] == a2
    items.append({"id": a2, "kind": "FR_ALLOCATION"})
    info = check(client, PROJECT, items); assert not info["blocked"]
    assert len(info["items"][1]["allocation_targets"]) == 2 and not info["items"][3]["allocation_targets"]
    directory = db.execute("SELECT count(*) FROM ee.subsystems WHERE project_id=%s", (PROJECT,)).fetchone()[0]
    result = submit(client, PROJECT, request(info))
    assert [i["kind"] for i in result["items"]] == ["FR_ALLOCATION", "FR_ALLOCATION", "FR", "FR"]
    assert_active(db, [prd["artifact_id"], uc["artifact_id"]])
    assert db.execute("SELECT count(*) FROM ee.subsystems WHERE project_id=%s", (PROJECT,)).fetchone()[0] == directory


def test_preview_filters_internal_dependencies_before_truncation(client, db, resource):
    p, f = resource; pid = p["id"]
    frs = [create(client, pid, f, "FR", description=f"对象{i}") for i in range(24)]
    items = selection(f, *(("FR", fr) for fr in frs))
    assert not check(client, pid, items)["blocked"]
    excluded = items.pop()
    preview = check(client, pid, items)
    assert preview["items"][0]["blocker_count"] == 1
    assert preview["items"][0]["blockers"][0]["id"] == excluded["id"]
    submit(client, pid, request(preview), 409)
    assert_active(db, [fr["artifact_id"] for fr in frs])


def test_stale_version_rolls_back_all_and_refresh_allows_retry(client, db, resource):
    p, f = resource; pid = p["id"]
    fr1 = create(client, pid, f, "FR", description="A"); fr2 = create(client, pid, f, "FR", description="B")
    info = check(client, pid, selection(f, ("FR", fr1), ("FR", fr2))); payload = request(info)
    save_fr(client, pid, fr2, [])
    submit(client, pid, payload, 409)
    assert_active(db, [fr1["artifact_id"], fr2["artifact_id"]])
    assert db.execute("SELECT count(*) FROM ee.security_audit_events WHERE id=%s", (payload["mutation_id"],)).fetchone()[0] == 0
    submit(client, pid, request(check(client, pid, selection(f, ("FR", fr1), ("FR", fr2)))))


def test_audit_failure_after_updates_rolls_back_every_object(client, db, resource):
    p, f = resource; pid = p["id"]; prd, uc, fr = chain(client, pid, f)
    payload = request(check(client, pid, selection(f, ("PRD", prd), ("UC", uc), ("FR", fr))))
    db.execute("ALTER TABLE ee.security_audit_events ADD CONSTRAINT batch_test_failure CHECK(event_type<>'batch.delete') NOT VALID")
    try:
        submit(client, pid, payload, 409)
        assert_active(db, [prd["artifact_id"], uc["artifact_id"], fr["artifact_id"]])
        assert db.execute("SELECT archived_at FROM ee.business_functions WHERE id=%s", (f,)).fetchone()[0] is None
        assert db.execute("SELECT count(*) FROM ee.security_audit_events WHERE id=%s", (payload["mutation_id"],)).fetchone()[0] == 0
    finally:
        db.execute("ALTER TABLE ee.security_audit_events DROP CONSTRAINT batch_test_failure")
    submit(client, pid, payload)


def test_cross_function_same_project_allowed_other_project_type_or_stale_forbidden(client, db, resource):
    p, f = resource; pid = p["id"]
    f2 = post(client, f'/{pid}/functions', {"creation_id": str(uuid4()), "name": "第二功能"})["function_id"]
    a = create(client, pid, f, "FR", description="A"); b = create(client, pid, f2, "FR", description="B")
    items = selection(f, ("FR", a)) + selection(f2, ("FR", b))
    wrong = [{"id": a["artifact_id"], "kind": "PRD"}]; check(client, pid, wrong, 404)
    other = post(client, '', {"creation_id": str(uuid4()), "name": "另一项目"})
    check(client, other["id"], items, 404)
    payload = request(check(client, pid, items))
    submit(client, other["id"], payload, 404)
    submit(client, pid, payload)


def test_new_child_after_preview_blocks_batch(client, db, resource):
    p, f = resource; pid = p["id"]; payload = request(check(client, pid, selection(f)))
    child = create(client, pid, f, "FR", description="并发创建")
    submit(client, pid, payload, 409); assert_active(db, [child["artifact_id"]])


@pytest.mark.parametrize("first", ["delete", "save"])
def test_ordered_batch_vs_save_serializes_on_project(client, db, resource, monkeypatch, first):
    p, f = resource; pid = p["id"]; fr = create(client, pid, f, "FR", description="原始")
    payload = request(check(client, pid, [{"id": fr["artifact_id"], "kind": "FR"}]))
    entered, waiting, release = Event(), Event(), Event()
    holder = bulk_deletion if first == "delete" else manual_work
    waiter = manual_work if first == "delete" else bulk_deletion
    original, other = holder.lock_project, waiter.lock_project
    def held(*args, **kw):
        result = original(*args, **kw); entered.set(); assert release.wait(10); return result
    def arriving(*args, **kw):
        waiting.set(); return other(*args, **kw)
    monkeypatch.setattr(holder, "lock_project", held); monkeypatch.setattr(waiter, "lock_project", arriving)
    def operation(kind):
        with TestClient(app) as c:
            c.cookies.set("ts_session", create_session_token("engineer01"))
            if kind == "delete": return c.post(f"/v1/projects/{pid}/deletion-batch", headers=HEADERS, json=payload).status_code
            return c.post(f"/v1/projects/{pid}/artifacts/{fr['artifact_id']}/revisions", headers=HEADERS, json={"mutation_id": str(uuid4()), "base_revision_id": fr["revision_id"], "content": {"kind": "FR", "description": "保存", "sources": []}}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(operation, first); assert entered.wait(10)
        b = pool.submit(operation, "save" if first == "delete" else "delete"); assert waiting.wait(10); release.set()
        statuses = (a.result(), b.result())
    assert statuses == ((200, 404) if first == "delete" else (201, 409))


def test_batch_current_head_accepted_and_exclusively_historical_references(client, db, resource):
    p, f = resource; pid = p['id']; prd, u1, fr1 = chain(client, pid, f)
    u2 = create(client, pid, f, 'UC', name='UC2', description='新场景', main_flow='流程', prd_revision_id=prd['revision_id']); accept(client, pid, u2)
    fr2 = save_fr(client, pid, fr1, [u2])
    both = [{'id': u['artifact_id'], 'kind': 'UC'} for u in (u1, u2)]
    preview = check(client, pid, both)
    assert preview['items'][0]['blockers'][0]['reference_versions'] == ['accepted']
    assert preview['items'][1]['blockers'][0]['reference_versions'] == ['head']
    submit(client, pid, request(preview), 409)
    assert_active(db, [u1['artifact_id'], u2['artifact_id'], fr1['artifact_id']])
    accept(client, pid, fr2)
    historical = check(client, pid, [both[0]])
    assert not historical['blocked']; submit(client, pid, request(historical))
    assert db.execute('SELECT count(*) FROM ee.fr_uc_sources WHERE fr_revision_id=%s AND uc_id=%s', (fr1['revision_id'], u1['artifact_id'])).fetchone()[0] == 1
    assert_active(db, [u2['artifact_id'], fr1['artifact_id']])


def test_concurrent_duplicate_batch_commits_one_audit(client, db, resource):
    p, f = resource; pid = p['id']; fr = create(client, pid, f, 'FR', description='并发重试')
    payload = request(check(client, pid, selection(f, ('FR', fr))))
    def send():
        with TestClient(app) as c:
            c.cookies.set('ts_session', create_session_token('engineer01'))
            return submit(c, pid, payload)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(send), pool.submit(send)
        assert a.result() == b.result()
    assert db.execute('SELECT count(*) FROM ee.security_audit_events WHERE id=%s', (payload['mutation_id'],)).fetchone()[0] == 1


def test_single_and_batch_mutation_ids_cannot_be_reused_between_operations(client, db, resource):
    from tests.test_deletion_postgres import erase
    p, f = resource; pid = p['id']
    a=create(client,pid,f,'FR',description='单条'); b=create(client,pid,f,'FR',description='批次')
    mid=str(uuid4())
    erase(client, f'/{pid}/artifacts/{a["artifact_id"]}', mid=mid)
    payload=request(check(client,pid,[{'id':b['artifact_id'],'kind':'FR'}]),mid)
    submit(client,pid,payload,409); assert_active(db,[b['artifact_id']])
    payload['mutation_id']=str(uuid4()); result=submit(client,pid,payload)
    c=create(client,pid,f,'FR',description='保留')
    erase(client,f'/{pid}/artifacts/{c["artifact_id"]}',mid=payload['mutation_id'],status=409)
    assert_active(db,[c['artifact_id']]); assert result['deleted']
