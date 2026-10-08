# Backend Agent Rules

## Scope

- This repository implements the Python backend for the TS Agent platform.
-一期目标是 FastAPI 健康检查、登录、获取当前用户、退出登录和 OpenAPI 文档。
- 2026-09-21 用户明确授权新增且仅新增 PRD 自动扩写转接接口；不增加通用 Agent 调度、其他生成接口、文件存储、权限系统或数据库。

## Architecture

- Use FastAPI with Uvicorn.
- API routes belong in `app/api/routes`.
- Pydantic request/response models belong in `app/schemas`.
- Business logic belongs in services; routes should remain thin.
- Configuration belongs in `app/core` and comes from environment variables.
- API paths use the `/v1` prefix.

## Authentication

- Use HttpOnly cookies for the demo session.
- Never ask the frontend to store auth tokens in localStorage or sessionStorage.
- Demo credentials come from environment variables and must not be committed.
- Use `Secure=true` in production and `SameSite=Lax` unless deployment requirements require a documented change.

## API Contract

- FastAPI OpenAPI is the source of truth for frontend types.
- Keep response shapes stable and document intentional breaking changes.
- Include a request ID in API responses or logs when the middleware is introduced.
- Do not add task queues, Agent registries, or workflow engines in一期.

## Quality Gate

Before marking a task complete, run:

```text
pytest
```

Also verify `/health`, `/docs`, `/redoc`, login, `/me`, and logout manually, then update the shared progress file in the frontend repository.

## Collaboration

- Auth/API Agent may modify only this repository.
- Do not modify frontend files from backend-only tasks. Exception: the user explicitly authorized the paired frontend/backend PRD expansion integration on 2026-09-21.
- Contract changes must be communicated before the frontend generated client is regenerated.
- Keep commits small and focused on one module.

## 2026-09-22 project-read authorization

- User explicitly authorized reuse/rename of the existing sample project to 测试项目1, its only function to 行车记录仪DVR, and backend project READ endpoints with tests. Reuse the deployed ee.projects table; no C/U/D API, no new business tables, no Agent changes.
- Backend edits for this scope and shared frontend progress/runbook documentation are authorized; frontend workspaces/generated clients are not being changed in this task. Company SQL remains user-executed through the reviewed independent package, not automatic SSH/restarts.


## 2026-09-22 project-create authorization (supersedes earlier READ-only project scope)

- User confirmed one empty-project creation flow in the switcher, POST /v1/projects and a list capability. Paired frontend/backend edits and isolated PostgreSQL testing are authorized.
- Reuse existing projects, project_members, access_bindings, project_subsystem_designs and security_audit_events; no new business tables, no project update/delete, no content generation or copying.
- Keep ee_project_api_ro SELECT-only. A separately provisioned INSERT service role is required; company operations remain user-executed through a new reviewed package, never automatic SSH. Frozen packages must not change.

## 2026-09-23 manual-work authorization

- User approved paired frontend/backend function, PRD, UC and FR manual creation, draft editing/saving and explicit acceptance under existing R2 rules. Reuse existing tables and the single ee_project_api_rw writer (renamed from ee_project_api_create); ee_project_api_ro stays SELECT-only.
- Existing frozen database packages remain unchanged. Company database operations remain user-executed from a reviewed incremental package; isolated local PostgreSQL tests are authorized.


## 2026-09-24 writer-rename and company-operation boundary

- User confirmed in-place rename ee_project_api_create -> ee_project_api_rw; two shared API logins (ro/rw), not one per feature/project. Preserve frozen packages and credentials; reject incompatible password formats before rename.
- Do not directly connect to company databases, SSH into company servers, or execute company migrations. Provide reviewed step-by-step commands for the user, starting with read-only inspection and waiting for its output before writes. Disposable local PostgreSQL and local browser tests remain authorized.

## 2026-09-28 staged CRUD authorization — stage 1 only

- User approved the five-stage CRUD plan and explicitly said “开始”. Stage 1 authorizes paired frontend/backend project/function metadata editing, read capabilities and version tokens, confirmed terminology, focused tests and an independent minimal SQL grant increment. Reuse existing tables, ro/rw logins and project author/reader boundaries; do not develop permission administration.
- Nonempty-project deletion is forbidden; historical-reference deletion checks cover current head and accepted versions, not only older history. These rules are confirmed for later stages, NOT authorization to implement deletion in stage 1.
- Complete implementation, local isolated PostgreSQL/browser verification and documentation for stage 1, then STOP and report for user acceptance. Do not start stage 2 until the user explicitly accepts stage 1. No company database/SSH/deployment, and frozen packages must remain unchanged.

## 2026-09-28 staged CRUD authorization — stage 2 only

- User explicitly accepted stage 1 and authorized the next stage. Stage 2 covers backend single-object deletion/impact, active head+accepted references, concurrency/ancestor validation, active-PRD uniqueness and new-ID recreation, minimal independent SQL and focused read metadata/tests. Project/function/content edits from stage 1 remain supported.
- Do not implement sidebar delete UI (stage 3), bulk delete APIs/UI (stage 4), restore/recycle bin, permissions administration or new Agent flows. Complete stage 2, verify in disposable local databases and STOP for user acceptance.
- Existing local manual preview stays separate; do not migrate its database or stop it implicitly. No company DB/SSH/deployment. All previously delivered SQL packages, including metadata-edit-20260928, remain unchanged.

## 2026-09-28 staged CRUD authorization — stages 3 and 4 together

- User explicitly started stage 3, then authorized continuing directly into stage 4 for joint acceptance. This supersedes the stage-2 stop rule only for sidebar single deletion, truthful allocation navigation/UI synchronization, and explicit-selection atomic batch deletion with focused local tests. Stop after stage 4; stage 5 is NOT authorized.
- Do not update `E:/Git projects/TS-frontend/docs/current/25_工作交接_项目功能PRD_UC_FR_CRUD_20260928.md`. Maintain current interaction, progress and verification documents instead.
- Keep the original local acceptance services (3000/8000/56430) untouched. Use isolated disposable databases or separate stage-3/4 preview ports. No company database, SSH, deployment, credential changes or edits to frozen SQL packages. No restore/recycle bin, permission administration, complete allocation CRUD or new Agent flows.


## 2026-09-28 staged CRUD authorization — stage 5

- User explicitly continued with “第五部分，继续” on 2026-09-28. This supersedes the prior stage-3/4 stop rule and authorizes stage 5 only: full local/isolated CRUD regression, browser/API verification, SQL package/hash/rollback-boundary review, and current execution-package documentation.
- Keep the confirmed scope: project/function/PRD/UC/FR and FR-allocation deletion behavior, dependency guards, active head/accepted reference checks, PRD delete-then-new-ID creation, batch atomicity and failure recovery. Do not count complete FR-allocation create/edit/accept CRUD as implemented.
- Do not connect to company databases, SSH, deploy, restart company services, change company credentials/configuration/bindings, modify frozen SQL packages, add permission administration, add restore/recycle-bin UI, or add new Agent flows. The user executes company materials and returns safe receipts for review.
- Do not modify `E:/Git projects/TS-frontend/docs/current/25_工作交接_项目功能PRD_UC_FR_CRUD_20260928.md`. Stage 5 ends after the local/isolated evidence and documentation are delivered; stop and wait for final user acceptance.
