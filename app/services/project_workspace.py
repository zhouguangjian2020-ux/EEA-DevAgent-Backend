"""Read-only aggregation for the existing project workspace data."""
from collections import defaultdict
from uuid import UUID

import psycopg

from app.core.database import (
    ProjectDatabaseNotConfigured,
    UnsafeProjectDatabaseRole,
    project_connection,
)
from app.schemas.project_workspace import (
    ProjectWorkspaceResponse,
    WorkspaceAllocation,
    WorkspaceAllocationTarget,
    WorkspaceFr,
    WorkspaceFrSource,
    WorkspaceFunction,
    WorkspacePrd,
    WorkspaceRevision,
    WorkspaceSubsystem,
    WorkspaceUc,
)
from app.schemas.projects import ProjectRead
from app.services.projects import ProjectReadError, _SELECT, project_read
from app.services.business_access import capabilities

_BRANCH_ORDER = ("a1", "a2", "a3", "a4")
_EXCEPTION_ORDER = ("vehicle_state", "mechanical", "environmental", "misuse", "power")


def _scenario_bodies(rows: list[dict], order: tuple[str, ...], limit: int) -> list[str]:
    # The DB permits one item per kind/category. Empty categories keep their UI slot.
    bodies = {row["category"]: row["body"] for row in rows}
    return [bodies.get(category, "") for category in order[:limit]]


def _revision(row: dict) -> WorkspaceRevision:
    return WorkspaceRevision(
        revision_id=row["revision_id"],
        revision_no=str(row["revision_no"]),
        status="accepted" if row["accepted_revision_id"] == row["revision_id"] else "pending",
                accepted_revision_id=row["accepted_revision_id"],
        type_version_id=row["type_version_id"],
        created_via=row["created_via"],
        created_at=row["created_at"],
    )


def _database_error(error: Exception) -> ProjectReadError:
    if isinstance(error, ProjectDatabaseNotConfigured):
        return ProjectReadError(503, "projects_not_configured", "项目数据库尚未配置")
    if isinstance(error, UnsafeProjectDatabaseRole):
        return ProjectReadError(503, "projects_unsafe_database_role", "项目接口需要专用低权限数据库账号")
    if isinstance(error, (psycopg.errors.QueryCanceled, psycopg.errors.LockNotAvailable)):
        return ProjectReadError(504, "projects_query_timeout", "项目查询超时，请稍后重试")
    return ProjectReadError(503, "projects_database_unavailable", "项目数据库暂不可用")


def _read_workspace(subject: str, project_id: UUID) -> ProjectWorkspaceResponse:
    try:
        with project_connection(repeatable_read=True) as connection:
            project_rows = connection.execute(_SELECT + " AND p.id = %s", (subject, project_id)).fetchall()
            if not project_rows:
                raise ProjectReadError(404, "project_not_found", "项目不存在或无权访问")
            project = project_read(project_rows[0])
            allowed = capabilities(connection, subject, project_id)
            accepted_ucs = connection.execute("""
                SELECT a.function_id, a.id AS uc_id, a.code AS uc_code, uv.name AS uc_name,
                       a.accepted_revision_id AS uc_revision_id
                FROM ee.artifacts a JOIN ee.uc_versions uv ON uv.revision_id=a.accepted_revision_id
                WHERE a.project_id=%s AND a.type_code='UC' AND a.archived_at IS NULL
                ORDER BY a.code, a.id
            """, (project_id,)).fetchall()

            functions = connection.execute(
                """
                SELECT id, code, name, description, lock_version::text AS lock_version
                FROM ee.business_functions
                WHERE project_id = %s AND archived_at IS NULL
                ORDER BY created_at, id
                """,
                (project_id,),
            ).fetchall()

            workflow_caps = {row['id']: sorted(capabilities(connection, subject, project_id, row['id'])) for row in functions}
            all_artifacts = connection.execute(
                """
                SELECT a.id, a.function_id, a.code, a.type_code, a.lock_version::text AS lock_version,
                       a.head_revision_id, a.accepted_revision_id,
                       r.revision_no, r.type_version_id, r.created_via, r.created_at
                FROM ee.artifacts a
                LEFT JOIN ee.artifact_revisions r ON r.id = a.head_revision_id
                WHERE a.project_id = %s AND a.archived_at IS NULL
                ORDER BY a.created_at, a.id
                """,
                (project_id,),
            ).fetchall()
            artifacts = {(row["id"], row["type_code"]): row for row in all_artifacts}

            prd_rows = connection.execute(
                """
                SELECT a.id, a.function_id, a.code, a.accepted_revision_id,
                       r.id AS revision_id, r.revision_no, r.type_version_id,
                       r.created_via, r.created_at, pv.markdown_body
                FROM ee.artifacts a
                JOIN ee.prds p ON p.artifact_id = a.id AND p.project_id = a.project_id
                JOIN ee.artifact_revisions r ON r.id = a.head_revision_id
                JOIN ee.prd_versions pv ON pv.revision_id = r.id
                WHERE a.project_id = %s AND a.type_code = 'PRD' AND a.archived_at IS NULL
                ORDER BY a.function_id, r.revision_no DESC
                """,
                (project_id,),
            ).fetchall()

            uc_rows = connection.execute(
                """
                SELECT a.id, a.function_id, a.code, a.accepted_revision_id,
                       r.id AS revision_id, r.revision_no, r.type_version_id,
                       r.created_via, r.created_at,
                       uv.prd_revision_id, uv.name, uv.description, uv.actors_text,
                       uv.preconditions, uv.main_flow, uv.postconditions,
                       uv.extensions_text, uv.hmi_logic, uv.regulations,
                       uv.performance, uv.nonfunctional
                FROM ee.artifacts a
                JOIN ee.use_cases u ON u.artifact_id = a.id AND u.project_id = a.project_id
                JOIN ee.artifact_revisions r ON r.id = a.head_revision_id
                JOIN ee.uc_versions uv ON uv.revision_id = r.id
                WHERE a.project_id = %s AND a.type_code = 'UC' AND a.archived_at IS NULL
                ORDER BY a.function_id, a.created_at, a.id
                """,
                (project_id,),
            ).fetchall()
            scenario_rows = connection.execute(
                """
                SELECT uc_revision_id, kind, category, position, item_id, body
                FROM ee.uc_scenario_items
                WHERE project_id = %s
                ORDER BY uc_revision_id, position, item_id
                """,
                (project_id,),
            ).fetchall()

            fr_rows = connection.execute(
                """
                SELECT a.id, a.function_id, a.code, a.accepted_revision_id,
                       r.id AS revision_id, r.revision_no, r.type_version_id,
                       r.created_via, r.created_at, fv.description, r.metadata->>'acceptance_criteria' AS acceptance_criteria
                FROM ee.artifacts a
                JOIN ee.functional_requirements f ON f.artifact_id = a.id AND f.project_id = a.project_id
                JOIN ee.artifact_revisions r ON r.id = a.head_revision_id
                JOIN ee.fr_versions fv ON fv.revision_id = r.id
                WHERE a.project_id = %s AND a.type_code = 'FR' AND a.archived_at IS NULL
                ORDER BY a.function_id, a.created_at, a.id
                """,
                (project_id,),
            ).fetchall()
            source_rows = connection.execute(
                """
                SELECT s.fr_revision_id, s.uc_id, s.uc_revision_id, s.rationale,
                       ua.code AS uc_code, uv.name AS uc_name
                FROM ee.fr_uc_sources s
                JOIN ee.artifacts ua ON ua.id = s.uc_id AND ua.project_id = s.project_id
                JOIN ee.uc_versions uv ON uv.revision_id = s.uc_revision_id
                WHERE s.project_id = %s
                ORDER BY s.fr_revision_id, ua.code, s.uc_id
                """,
                (project_id,),
            ).fetchall()

            subsystem_rows = connection.execute(
                """
                SELECT s.id, s.code, i.name, i.description,
                       i.responsibility_in, i.responsibility_out, i.domain_group,
                       i.is_allocatable, i.sort_order, i.design_version_id,
                       dv.version_no AS design_version_no
                FROM ee.project_subsystem_designs d
                JOIN ee.subsystem_design_versions dv
                  ON dv.id = d.current_version_id AND dv.project_id = d.project_id
                JOIN ee.subsystem_version_items i
                  ON i.design_version_id = dv.id AND i.project_id = dv.project_id
                JOIN ee.subsystems s
                  ON s.id = i.subsystem_id AND s.project_id = i.project_id
                WHERE d.project_id = %s
                ORDER BY i.sort_order, s.code, s.id
                """,
                (project_id,),
            ).fetchall()

            allocation_rows = connection.execute(
                """
                SELECT a.id, a.function_id, a.code AS allocation_code,
                       a.accepted_revision_id,
                       r.id AS revision_id, r.revision_no, r.type_version_id,
                       r.created_via, r.created_at,
                       av.fr_id, av.fr_revision_id, av.design_version_id, av.note,
                       dv.version_no AS design_version_no,
                       at.subsystem_id, at.rationale,
                       s.code AS subsystem_code, i.name AS subsystem_name,
                       i.domain_group
                FROM ee.artifacts a
                JOIN ee.fr_allocations fa ON fa.artifact_id = a.id AND fa.project_id = a.project_id
                JOIN ee.artifact_revisions r ON r.id = a.head_revision_id
                JOIN ee.allocation_versions av ON av.revision_id = r.id
                JOIN ee.subsystem_design_versions dv
                  ON dv.id = av.design_version_id AND dv.project_id = av.project_id
                LEFT JOIN ee.allocation_targets at
                  ON at.allocation_revision_id = av.revision_id
                 AND at.design_version_id = av.design_version_id
                 AND at.project_id = av.project_id
                LEFT JOIN ee.subsystems s
                  ON s.id = at.subsystem_id AND s.project_id = at.project_id
                LEFT JOIN ee.subsystem_version_items i
                  ON i.subsystem_id = at.subsystem_id
                 AND i.design_version_id = at.design_version_id
                 AND i.project_id = at.project_id
                WHERE a.project_id = %s AND a.type_code = 'FR_ALLOCATION' AND a.archived_at IS NULL
                ORDER BY a.function_id, av.fr_id, at.subsystem_id
                """,
                (project_id,),
            ).fetchall()

    except ProjectReadError:
        raise
    except (ProjectDatabaseNotConfigured, UnsafeProjectDatabaseRole, psycopg.Error, ValueError) as error:
        raise _database_error(error) from error

    scenario_by_revision: dict[UUID, dict[str, list[dict]]] = defaultdict(lambda: {"branch": [], "exception": []})
    for row in scenario_rows:
        scenario_by_revision[row["uc_revision_id"]][row["kind"]].append(row)
    source_by_revision: dict[UUID, list[WorkspaceFrSource]] = defaultdict(list)
    for row in source_rows:
        source_by_revision[row["fr_revision_id"]].append(
            WorkspaceFrSource(
                uc_id=row["uc_id"], uc_code=row["uc_code"], uc_name=row["uc_name"],
                uc_revision_id=row["uc_revision_id"], rationale=row["rationale"],
            )
        )

    prd_by_function: dict[UUID, WorkspacePrd] = {}
    for row in prd_rows:
        artifact = artifacts[(row["id"], "PRD")]
        prd_by_function[row["function_id"]] = WorkspacePrd(
                lock_version=artifacts[(row["id"], "PRD")]["lock_version"], can_delete="PRD.archive" in allowed,
            id=row["id"], code=row["code"],
            revision=WorkspaceRevision(
                revision_id=row["revision_id"], revision_no=str(row["revision_no"]),
                status="accepted" if row["accepted_revision_id"] == row["revision_id"] else "pending",
                accepted_revision_id=row["accepted_revision_id"],
                type_version_id=row["type_version_id"], created_via=row["created_via"], created_at=row["created_at"],
            ),
            markdown_body=row["markdown_body"],
        )

    ucs_by_function: dict[UUID, list[WorkspaceUc]] = defaultdict(list)
    for row in uc_rows:
        scenarios = scenario_by_revision[row["revision_id"]]
        branches = _scenario_bodies(scenarios["branch"], _BRANCH_ORDER, 4)
        exceptions = _scenario_bodies(scenarios["exception"], _EXCEPTION_ORDER, 5)
        artifact = artifacts[(row["id"], "UC")]
        ucs_by_function[row["function_id"]].append(
            WorkspaceUc(
                lock_version=artifacts[(row["id"], "UC")]["lock_version"], can_delete="UC.archive" in allowed,
                id=row["id"], code=row["code"],
                revision=WorkspaceRevision(
                    revision_id=row["revision_id"], revision_no=str(row["revision_no"]),
                    status="accepted" if row["accepted_revision_id"] == row["revision_id"] else "pending",
                accepted_revision_id=row["accepted_revision_id"],
                    type_version_id=row["type_version_id"], created_via=row["created_via"], created_at=row["created_at"],
                ),
                prd_revision_id=row["prd_revision_id"], name=row["name"], description=row["description"],
                actors=row["actors_text"], preconditions=row["preconditions"], main_flow=row["main_flow"],
                branches=branches, exceptions=exceptions, postconditions=row["postconditions"],
                extensions=row["extensions_text"], hmi=row["hmi_logic"], regulations=row["regulations"],
                performance=row["performance"], nonfunctional=row["nonfunctional"],
            )
        )

    frs_by_function: dict[UUID, list[WorkspaceFr]] = defaultdict(list)
    fr_code_by_id: dict[UUID, str] = {}
    for row in fr_rows:
        fr_code_by_id[row["id"]] = row["code"]
        frs_by_function[row["function_id"]].append(
            WorkspaceFr(
                lock_version=artifacts[(row["id"], "FR")]["lock_version"], can_delete="FR.archive" in allowed,
                id=row["id"], code=row["code"],
                revision=WorkspaceRevision(
                    revision_id=row["revision_id"], revision_no=str(row["revision_no"]),
                    status="accepted" if row["accepted_revision_id"] == row["revision_id"] else "pending",
                accepted_revision_id=row["accepted_revision_id"],
                    type_version_id=row["type_version_id"], created_via=row["created_via"], created_at=row["created_at"],
                ),
                description=row["description"], sources=source_by_revision[row["revision_id"]],
                acceptance_criteria=row.get("acceptance_criteria") or "",
            )
        )

    subsystems = [WorkspaceSubsystem(**row) for row in subsystem_rows]
    allocations_by_function: dict[UUID, dict[UUID, WorkspaceAllocation]] = defaultdict(dict)
    for row in allocation_rows:
        existing = allocations_by_function[row["function_id"]].get(row["id"])
        if existing is None:
            existing = WorkspaceAllocation(
                lock_version=artifacts[(row["id"], "FR_ALLOCATION")]["lock_version"], can_delete="FR_ALLOCATION.archive" in allowed,
                id=row["id"], fr_id=row["fr_id"], fr_code=fr_code_by_id.get(row["fr_id"], ""),
                revision=WorkspaceRevision(
                    revision_id=row["revision_id"], revision_no=str(row["revision_no"]),
                    status="accepted" if row["accepted_revision_id"] == row["revision_id"] else "pending",
                accepted_revision_id=row["accepted_revision_id"],
                    type_version_id=row["type_version_id"], created_via=row["created_via"], created_at=row["created_at"],
                ),
                fr_revision_id=row["fr_revision_id"], design_version_id=row["design_version_id"],
                design_version_no=row["design_version_no"], note=row["note"], targets=[],
            )
        if row["subsystem_id"] is not None:
            existing.targets.append(
                WorkspaceAllocationTarget(
                    subsystem_id=row["subsystem_id"], subsystem_name=row["subsystem_name"],
                    subsystem_code=row["subsystem_code"], domain_group=row["domain_group"],
                    rationale=row["rationale"],
                )
            )
        allocations_by_function[row["function_id"]][row["id"]] = existing

    function_models = []
    for row in functions:
        function_models.append(
            WorkspaceFunction(
                workflow_capabilities=workflow_caps[row['id']],
                id=row["id"], code=row["code"], name=row["name"], description=row["description"],
                lock_version=row["lock_version"], can_update="function.update" in allowed, can_delete="function.archive" in allowed,
                prd=prd_by_function.get(row["id"]),
                can_create_prd="PRD.create" in allowed and row["id"] not in prd_by_function, can_create_uc="UC.create" in allowed,
                can_create_fr="FR.create" in allowed,
                accepted_use_cases=[WorkspaceFrSource(**uc) for uc in accepted_ucs if uc["function_id"] == row["id"]],
                use_cases=ucs_by_function[row["id"]],
                functional_requirements=frs_by_function[row["id"]],
                subsystems=subsystems,
                fr_allocations=list(allocations_by_function[row["id"]].values()),
            )
        )

    return ProjectWorkspaceResponse(project=project.model_dump(mode="json"), functions=function_models,
                                    can_create_function="function.create" in allowed, capabilities=sorted(allowed), read_only=not any(c.endswith((".create", ".update", ".accept", ".archive")) for c in allowed))


def get_project_workspace(subject: str, project_id: UUID) -> ProjectWorkspaceResponse:
    return _read_workspace(subject, project_id)


