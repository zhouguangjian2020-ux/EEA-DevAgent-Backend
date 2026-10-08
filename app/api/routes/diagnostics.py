"""Private diagnostic reads: shared service key AND the original signed session."""
import asyncio
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, HTTPException, Request, Query

from app.core.config import settings
from app.core.database import project_connection
from app.core.diagnostic_http import cookie_subject, key_valid
from app.core.diagnostics import snapshot

router = APIRouter()


def owner(request: Request) -> str:
    if not key_valid(request.headers.get("x-ts-lab-key", "")):
        raise HTTPException(404, "未启用或无权访问联调采集")
    subject = cookie_subject(dict(request.headers))
    if not subject:
        raise HTTPException(401, "需要有效业务登录态")
    return subject


@router.get("/runs/{run_id}")
def get_run(run_id: str, request: Request, after: int = Query(0, ge=0)):
    value = snapshot(run_id, owner(request), after)
    if value is None:
        raise HTTPException(404, "记录不存在、已过期或不属于当前账号")
    return value


@router.get("/services")
async def services(request: Request):
    owner(request)
    def database_probe():
        if not settings.projects_database_url.get_secret_value():
            return {"status": "not_configured"}
        try:
            with project_connection() as connection:
                connection.execute("SELECT 1 AS ok").fetchone()
            return {"status": "healthy", "mode": "read_only"}
        except Exception as error:
            return {"status": "unavailable", "error_type": type(error).__name__}

    async def agent_probe():
        target = settings.agent_generation_invoke_url or settings.agent_invoke_url
        key = settings.agent_api_key.get_secret_value()
        parsed = urlsplit(target)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or not key or parsed.username or parsed.password:
            return {"status": "not_configured"}
        path = parsed.path.removesuffix("/stream").rstrip("/")
        url = urlunsplit((parsed.scheme, parsed.netloc, path.rsplit("/", 1)[0] + "/capabilities", "", ""))
        try:
            async with httpx.AsyncClient(timeout=5, trust_env=False, follow_redirects=False) as client:
                async with client.stream("GET", url, headers={"X-API-Key": key}) as response:
                    # Reachability/authentication only; 200 is NOT proof of a working skill.
                    return {"status": "reachable" if response.status_code == 200 else "upstream_rejected", "http_status": response.status_code, "skill_readiness": "not_verified"}
        except httpx.HTTPError:
            return {"status": "unreachable", "skill_readiness": "not_verified"}
    db, agent = await asyncio.gather(asyncio.to_thread(database_probe), agent_probe())
    return {"backend": {"status": "healthy"}, "database": db, "agent": agent, "capture": {"enabled": True, "storage": "bounded_memory", "ttl_seconds": 900, "deployment": "single_worker_required"}}
