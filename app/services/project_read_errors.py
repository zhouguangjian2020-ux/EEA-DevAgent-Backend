"""Sanitized database failure mapping shared by read-only project workflows."""
import psycopg

from app.core.database import ProjectDatabaseNotConfigured, UnsafeProjectDatabaseRole
from app.services.projects import ProjectReadError


def database_read_error(error: Exception) -> ProjectReadError:
    """Do not include DSNs, SQL, credentials, or raw database error text."""
    if isinstance(error, ProjectDatabaseNotConfigured):
        return ProjectReadError(503, "projects_not_configured", "项目数据库尚未配置")
    if isinstance(error, UnsafeProjectDatabaseRole):
        return ProjectReadError(503, "projects_unsafe_database_role", "项目接口需要专用低权限数据库账号")
    if isinstance(error, (psycopg.errors.QueryCanceled, psycopg.errors.LockNotAvailable)):
        return ProjectReadError(504, "projects_query_timeout", "项目查询超时，请稍后重试")
    return ProjectReadError(503, "projects_database_unavailable", "项目数据库暂不可用")
