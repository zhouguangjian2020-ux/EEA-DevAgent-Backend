"""Read-only functional-definition planning. Does not create or accept objects."""
import asyncio
from dataclasses import dataclass
from difflib import SequenceMatcher
import unicodedata
from uuid import UUID

from app.core.config import settings
from app.schemas.prd_creation import FeatureCandidate, FeatureDefinition, FeaturePlanReview
from app.services import workflow_agent
from app.services.business_access import capabilities
from app.services.function_names import normalize_name
from app.services.workflow_sources import digest, fail, read_db, visible

SCENE = "FEATURE_DEFINITION"
MAX_FUNCTIONS = 2000
MAX_CANDIDATES = 20
NAME_SIMILARITY = 0.78
DESCRIPTION_SIMILARITY = 0.85


def parse_definition(envelope: dict) -> FeatureDefinition:
    # Only this exact, documented source is trusted. A title in structured or
    # text is not a substitute for the user's confirmed plan.feature_name.
    if not isinstance(envelope, dict) or envelope.get("scene") != SCENE:
        fail(502, "agent_plan_invalid", "Agent功能计划场景不匹配，未创建功能")
    if envelope.get("error") is not None:
        fail(502, "agent_plan_invalid", "Agent返回错误，未创建功能")
    if envelope.get("interrupt") is not None:
        fail(502, "agent_plan_interrupted", "Agent功能计划未完成，请核对上游状态；未创建功能")
    plan = envelope.get("plan")
    name = plan.get("feature_name") if isinstance(plan, dict) else None
    description = envelope.get("output")
    if not isinstance(name, str) or not isinstance(description, str):
        fail(502, "agent_plan_invalid", "Agent缺少plan.feature_name或文本output，请核对非流式契约；未创建功能")
    if any(unicodedata.category(c).startswith("C") and not c.isspace() for c in name):
        fail(502, "agent_plan_invalid", "Agent功能名称含非法字符，未创建功能")
    name = " ".join(name.split())
    description = description.strip()
    if not 1 <= len(name) <= 100 or not 1 <= len(description) <= 2000 or "\x00" in description:
        fail(502, "agent_plan_invalid", "Agent功能名称或说明为空、过长或非法，未创建功能")
    return FeatureDefinition(name=name, description=description)


def description_grams(value: str) -> set[str]:
    text = normalize_name(value)
    if len(text) < 20:
        return set()
    return {text[i:i + 3] for i in range(len(text) - 2)}


def match_candidates(definition: FeatureDefinition, functions: list[dict]) -> list[FeatureCandidate]:
    """Deterministic suggestions, not semantic identity or permission decisions."""
    key = normalize_name(definition.name)
    description = description_grams(definition.description)
    ranked = []
    for row in functions:
        other = normalize_name(row["name"])
        score = 1.0
        if key == other:
            match, rank = "exact_name", 0
        else:
            matcher = SequenceMatcher(None, key, other, autojunk=False)
            score = matcher.ratio()
            common = matcher.find_longest_match().size
            # Recognize a qualified name such as 行车记录仪自动循环录制 without
            # treating that suggestion as proof of semantic equivalence.
            shared_name = common >= 4 and common / min(len(key), len(other)) >= 0.8
            if score >= NAME_SIMILARITY or shared_name:
                match, rank = "similar_name", 1
            else:
                grams = description_grams(row["description"] or "")
                union = description | grams
                score = len(description & grams) / len(union) if description and grams else 0
                if score < DESCRIPTION_SIMILARITY:
                    continue
                match, rank = "similar_description", 2
        existing = row["prd_artifact_id"]
        blocked = "prd_exists" if existing else None if row["can_create_prd"] else "prd_create_forbidden"
        candidate = FeatureCandidate(
            function_id=row["id"], name=row["name"], description=row["description"] or "",
            match=match, prd_artifact_id=existing,
            can_reuse_for_prd=blocked is None, blocked_reason=blocked,
        )
        ranked.append((rank, -score, other, str(row["id"]), candidate))
    return [item[-1] for item in sorted(ranked, key=lambda item: item[:4])]


@dataclass(frozen=True)
class PlanningSnapshot:
    can_create_new: bool
    functions: list[dict]


def read_planning_snapshot(subject: str, project_id: UUID) -> PlanningSnapshot:
    # This anticipates a write workflow. Preserve the disabled preview policy;
    # don't spend Agent calls for an unavailable downstream business operation.
    if not settings.business_writes_enabled:
        fail(503, "business_writes_disabled", "业务写入未启用，不能开始PRD自动创建")
    if not settings.projects_write_database_url.get_secret_value().strip():
        fail(503, "business_write_not_configured", "业务写入账号未配置，不能开始PRD自动创建")
    with read_db() as db:
        return planning_snapshot_in_db(db, subject, project_id)


def planning_snapshot_in_db(db, subject, project_id, *, require_creation_access=True):
    visible(db, subject, project_id)
    project_caps = capabilities(db, subject, project_id)
    functions = db.execute("""
        SELECT f.id, f.name, f.description, a.id AS prd_artifact_id
        FROM ee.business_functions f
        LEFT JOIN LATERAL (
            SELECT id FROM ee.artifacts
            WHERE project_id=f.project_id AND function_id=f.id
                AND type_code='PRD' AND archived_at IS NULL
            ORDER BY id LIMIT 1
        ) a ON true
        WHERE f.project_id=%s AND f.archived_at IS NULL
        ORDER BY f.created_at, f.id LIMIT %s
    """, (project_id, MAX_FUNCTIONS + 1)).fetchall()
    if len(functions) > MAX_FUNCTIONS:
        fail(413, "feature_catalog_too_large", "当前项目功能过多，未完成重复检查；请缩小范围，不会直接新建")
    # One scoped permission query rather than one extra query per function.
    scoped = set()
    if ("PRD.create" not in project_caps and settings.business_writes_enabled
            and settings.projects_write_database_url.get_secret_value().strip()):
        scoped = {row["function_id"] for row in db.execute("""
            SELECT DISTINCT b.function_id
            FROM ee.app_users u
            JOIN ee.project_members m ON m.user_id=u.id AND m.project_id=%s AND m.state='active'
            JOIN ee.access_bindings b ON b.user_id=u.id AND b.project_id=m.project_id
            JOIN ee.access_roles r ON r.id=b.role_id AND r.enabled
            JOIN ee.role_permissions rp ON rp.role_id=r.id
            JOIN ee.permission_definitions pd ON pd.code=rp.permission_code AND pd.enabled
            WHERE u.external_subject=%s AND u.state='active'
              AND b.scope_kind='function' AND 'function'=ANY(pd.allowed_scopes)
              AND pd.resource_kind='PRD' AND pd.action='create'
        """, (project_id, subject)).fetchall()}
    rows = [{**row, "can_create_prd": "PRD.create" in project_caps or row["id"] in scoped} for row in functions]
    can_create_new = {"function.create", "PRD.create"} <= project_caps
    if require_creation_access and not can_create_new and not any(row["can_create_prd"] for row in rows):
        fail(403, "prd_creation_forbidden", "当前账号无可用的功能或PRD创建权限")
    return PlanningSnapshot(can_create_new, rows)


async def plan_feature(subject, project_id, payload, request_id):
    # Permission check before spending an Agent request; no DB transaction spans
    # this awaited call. Re-read afterwards to catch revocation/concurrent PRDs.
    await asyncio.to_thread(read_planning_snapshot, subject, project_id)
    call_id = digest({"kind": "feature-plan-v1", "subject": subject, "project_id": str(project_id),
                      "request_id": str(payload.client_request_id), "message": payload.message})
    body = workflow_agent.request_body(payload.message, {"project_id": str(project_id)}, call_id, SCENE, "text")
    body["trace_id"] = request_id
    definition = parse_definition(await workflow_agent.invoke_envelope(body))
    snapshot = await asyncio.to_thread(read_planning_snapshot, subject, project_id)
    candidates = match_candidates(definition, snapshot.functions)
    return FeaturePlanReview(
        client_request_id=payload.client_request_id, project_id=project_id, definition=definition,
        status="needs_selection" if candidates else "ready_to_create" if snapshot.can_create_new else "blocked",
        candidates=candidates[:MAX_CANDIDATES], candidate_count=len(candidates),
        has_more=len(candidates) > MAX_CANDIDATES, can_create_new_function=snapshot.can_create_new,
        exact_name_conflict=any(c.match == "exact_name" for c in candidates),
    )
