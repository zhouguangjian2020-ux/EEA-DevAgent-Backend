"""Generation response snapshots must not reject or trim persisted UC scenarios."""
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.manual_work import UcContent
from app.schemas.workflows import GenerationSourceSnapshot, Modification


@pytest.mark.parametrize("branches,exceptions", [(0, 0), (1, 2), (4, 5), (7, 8)])
def test_generation_snapshot_preserves_all_uc_scenario_items(branches, exceptions):
    artifact, function, revision, prd = uuid4(), uuid4(), uuid4(), uuid4()
    content = {"kind": "UC", "prd_revision_id": str(prd), "branches": [f"分支 {i}" for i in range(branches)], "exceptions": [f"异常 {i}" for i in range(exceptions)]}
    value = GenerationSourceSnapshot(artifact_id=artifact, function_id=function, revision_id=revision, head_revision_id=revision,
                                     accepted_revision_id=revision, number="UC-TEST-001", status="accepted", content=content)
    actual = value.model_dump(mode="json")["content"]
    assert actual["branches"] == content["branches"]
    assert actual["exceptions"] == content["exceptions"]


def test_manual_edit_and_modification_contracts_are_not_relaxed():
    content = {"kind": "UC", "prd_revision_id": str(uuid4()), "branches": ["only one"], "exceptions": []}
    with pytest.raises(ValidationError):
        UcContent.model_validate(content)
    with pytest.raises(ValidationError):
        Modification(artifact_id=uuid4(), base_revision_id=uuid4(), content=content)
