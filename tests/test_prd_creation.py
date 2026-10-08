"""S2 contract tests: no real Agent, DB or company connections."""
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.prd_creation import FeaturePlanRequest, PrdCreationChoice
from app.services import prd_generation


def test_choice_rejects_untrusted_target_and_extra_fields():
    with pytest.raises(ValidationError):
        PrdCreationChoice(expected_version=1, action="create", function_id=uuid4())
    with pytest.raises(ValidationError):
        PrdCreationChoice(expected_version=1, action="reuse", name="fake")
    with pytest.raises(ValidationError):
        PrdCreationChoice(expected_version=1, action="continue", name="renamed")
    with pytest.raises(ValidationError):
        PrdCreationChoice(expected_version=1, action="create", definition={"name": "browser plan"})
    with pytest.raises(ValidationError):
        PrdCreationChoice(expected_version=1, action="create", name="hidden\u200b")


def test_original_prompt_is_isolated_from_existing_prd_stream_contract():
    context = prd_generation.PrdGenerationContext(uuid4(), "project", "summary", uuid4(), "feature", "details")
    original = prd_generation.build_prd_generation_request(context, version=None, request_id="test")
    augmented = prd_generation.build_prd_generation_request(
        context, version=None, request_id="test", original_prompt="source request", idempotency_key="retry-key")
    assert "original_prompt" not in original["reference_data"] and "idempotency_key" not in original
    assert augmented["response_format"] == "text"
    assert augmented["reference_data"]["original_prompt"] == "source request"
    assert augmented["idempotency_key"] == "retry-key"
    assert augmented["scene"] == original["scene"] == "FROM_DEFINITION_CREATE_PRD"


def test_request_is_validated_before_agent_call():
    with pytest.raises(ValidationError):
        FeaturePlanRequest(client_request_id=uuid4(), message="\x00")


@pytest.mark.parametrize("flag", ["error", "interrupt"])
def test_strict_prd_buffered_completion_rejects_partial_error_payload(flag):
    import json
    payload = {"scene": "FROM_DEFINITION_CREATE_PRD", "output": "partial", flag: {"note": "not complete"}}
    with pytest.raises(prd_generation.PrdGenerationError):
        prd_generation._validate_buffered_output(json.dumps(payload).encode(), strict_completion=True)


def test_routes_reject_anonymous_and_cross_origin_before_writes():
    from fastapi.testclient import TestClient
    from app.main import app
    from app.core.security import create_session_token
    url = f"/v1/projects/{uuid4()}/prd-creations/intents"
    body = {"client_request_id": str(uuid4()), "message": "plan"}
    with TestClient(app) as client:
        assert client.post(url, json=body).status_code == 401
        client.cookies.set("ts_session", create_session_token("engineer01"))
        assert client.post(url, json=body).status_code == 403
        assert client.post(url, json=body, headers={"Origin": "https://not-allowed.invalid", "X-Project-Request": "1"}).status_code == 403


def test_auto_action_cannot_force_renaming_or_reusing():
    with pytest.raises(ValidationError):
        PrdCreationChoice(action="auto", expected_version=1, name="forced")
    assert PrdCreationChoice(action="auto", expected_version=1).function_id is None
