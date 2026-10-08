from contextlib import contextmanager
import uuid

import pytest

from app.services import uc_generation
from app.services.projects import ProjectReadError


PROJECT = uuid.UUID("10d4c8a0-ff3a-56c3-abf6-18dc3b531e62")
FUNCTION = uuid.UUID("20d4c8a0-ff3a-56c3-abf6-18dc3b531e62")
PRD = uuid.UUID("30d4c8a0-ff3a-56c3-abf6-18dc3b531e62")
REVISION = uuid.UUID("40d4c8a0-ff3a-56c3-abf6-18dc3b531e62")
OTHER_REVISION = uuid.UUID("50d4c8a0-ff3a-56c3-abf6-18dc3b531e62")


class Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class Connection:
    def __init__(self, *, accepted_revision_id=REVISION, markdown_body="# PRD"):
        self.accepted_revision_id = accepted_revision_id
        self.markdown_body = markdown_body
        self.calls = []

    def execute(self, statement, params):
        self.calls.append((statement, params))
        if "FROM ee.business_functions" in statement:
            return Result({"id": FUNCTION})
        if "FROM ee.artifacts a" in statement and "JOIN ee.prds" in statement:
            return Result({"id": PRD, "function_id": FUNCTION, "accepted_revision_id": self.accepted_revision_id})
        if "FROM ee.artifact_revisions r" in statement:
            return Result({"markdown_body": self.markdown_body})
        return Result({"id": PROJECT})


@contextmanager
def database(connection):
    yield connection


def test_read_confirmed_prd_reads_only_server_side_accepted_body(monkeypatch):
    connection = Connection()
    monkeypatch.setattr(uc_generation, "project_connection", lambda repeatable_read=False: database(connection))
    result = uc_generation.read_confirmed_prd(
        "engineer01",
        project_id=PROJECT,
        function_id=FUNCTION,
        prd_id=PRD,
        revision_id=REVISION,
    )
    assert result.markdown_body == "# PRD"
    assert result.prd_id == PRD
    assert connection.calls[0][1] == ("engineer01", PROJECT)
    assert connection.calls[1][1] == (FUNCTION, PROJECT)
    assert connection.calls[2][1] == (PRD, PROJECT, FUNCTION)
    assert connection.calls[3][1] == (REVISION,)


def test_read_confirmed_prd_rejects_unconfirmed_revision(monkeypatch):
    connection = Connection(accepted_revision_id=None)
    monkeypatch.setattr(uc_generation, "project_connection", lambda repeatable_read=False: database(connection))
    with pytest.raises(ProjectReadError) as error:
        uc_generation.read_confirmed_prd(
            "engineer01",
            project_id=PROJECT,
            function_id=FUNCTION,
            prd_id=PRD,
            revision_id=REVISION,
        )
    assert error.value.status == 409
    assert error.value.code == "prd_not_confirmed"


def test_read_confirmed_prd_rejects_stale_revision(monkeypatch):
    connection = Connection(accepted_revision_id=OTHER_REVISION)
    monkeypatch.setattr(uc_generation, "project_connection", lambda repeatable_read=False: database(connection))
    with pytest.raises(ProjectReadError) as error:
        uc_generation.read_confirmed_prd(
            "engineer01",
            project_id=PROJECT,
            function_id=FUNCTION,
            prd_id=PRD,
            revision_id=REVISION,
        )
    assert error.value.status == 409
    assert error.value.code == "prd_revision_not_current"
