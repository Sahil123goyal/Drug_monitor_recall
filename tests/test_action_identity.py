"""An existing idempotent delivery cannot be relabelled after inventory editing."""

from unittest.mock import MagicMock
from uuid import UUID

import pytest

from app.agents.repository import WorkflowRepository
from app.models import Action, ActionStatus, Decision
from app.models import Notice
from langgraph.types import Command


@pytest.mark.parametrize("changed", [False, True])
def test_reusing_an_action_requires_the_same_medicine_snapshot(harness, changed):
    h = harness
    state = h["graph"].invoke(h["input"], h["config"])
    h["graph"].invoke(Command(resume={"confirmations": [
        {"decision_id": identifier, "accepted": True} for identifier in state["decision_ids"]
    ]}), h["config"])
    decision = h["repository"].decision(state["decision_ids"][0])
    snapshot = dict(decision.evidence["medicine_snapshot"])
    if changed:
        snapshot["batch_no"] = "PREVIOUS001"
    original = Decision(id=UUID(decision.id), evidence={**decision.evidence, "medicine_snapshot": snapshot})
    old = h["actions"][h["action_id"]]
    action = Action(id=UUID(old.id), decision_id=UUID(decision.id), status=ActionStatus.SENT,
                    idempotency_key=old.idempotency_key, proposed_message=old.proposed.model_dump_json(),
                    approved_message=old.proposed.model_dump_json())
    sessions = MagicMock()
    session = sessions.begin.return_value.__enter__.return_value
    session.scalar.return_value = action
    session.get.return_value = Notice(status=decision.evidence["notice_snapshot"]["status"],
                                     code_info=decision.evidence["notice_snapshot"]["code_info"])
    session.execute.return_value.all.return_value = [(action, original)]
    repository = WorkflowRepository(sessions)
    if changed:
        with pytest.raises(ValueError, match="previous details"):
            repository.propose(decision, "mock://synthetic")
    else:
        assert repository.propose(decision, "mock://synthetic").status == ActionStatus.SENT
