"""Shared buffered Agent endpoint validation, without a business-service dependency."""
from urllib.parse import urlsplit

from app.core.config import settings


class AgentConfigurationError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(code)


def generation_agent_url() -> str:
    configured = settings.agent_generation_invoke_url.strip()
    if configured:
        return configured
    configured = settings.agent_invoke_url.strip()
    if configured.endswith("/stream"):
        return configured[: -len("/stream")]
    return configured


def validate_generation_agent_configuration() -> str:
    # Preserve the existing public error text; extracting this shared validator
    # must not change any consumer's status, code, or response message.
    target_url = generation_agent_url()
    try:
        target = urlsplit(target_url)
    except ValueError as error:
        raise AgentConfigurationError(503, "agent_not_configured", "UC生成服务地址配置无效，请联系管理员") from error
    if (
        target.scheme not in {"http", "https"}
        or not target.hostname
        or target.username
        or target.password
        or target.query
        or target.fragment
        or not settings.agent_api_key.get_secret_value()
    ):
        raise AgentConfigurationError(503, "agent_not_configured", "UC生成服务尚未配置，请联系管理员")
    return target_url
