from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import agent, auth, health, projects, manual_work, deletion, permissions
from app.core.config import settings
from app.core.diagnostic_http import DiagnosticsMiddleware
from app.api.routes import diagnostics

app = FastAPI(
    title="TS Agent API",
    version="0.1.0",
    description="基础认证、PRD自动扩写、空项目/功能创建、工作空间查询，以及 PRD/UC/FR 手动创建、版本保存和显式确认。业务写入默认关闭，需部署授权和项目权限。",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(auth.router, prefix="/v1/auth", tags=["auth"])

app.include_router(agent.router, prefix="/v1/agent", tags=["agent"])

app.include_router(projects.router, prefix="/v1/projects", tags=["projects"])

app.include_router(manual_work.router, prefix="/v1/projects", tags=["manual-work"])
app.include_router(deletion.router, prefix="/v1/projects", tags=["deletion"])
app.include_router(permissions.router, tags=["permissions"])
app.add_middleware(DiagnosticsMiddleware)
app.include_router(diagnostics.router, prefix="/v1/agent/diagnostics", tags=["diagnostics"], include_in_schema=False)



from app.api.routes import workflows
app.include_router(workflows.router, prefix='/v1/projects', tags=['workflows'])

from app.api.routes import prd_creation
app.include_router(prd_creation.router, prefix="/v1/projects", tags=["prd-creation"])


# Outermost request timestamp; does not alter authentication or Agent payloads.
from app.core.request_timing import RequestTimingMiddleware
app.add_middleware(RequestTimingMiddleware)
