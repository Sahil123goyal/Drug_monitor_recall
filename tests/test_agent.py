"""Real LangGraph interrupts with in-memory checkpoints and mocked persistence."""

import pytest
from langgraph.types import Command

from app.agents.contracts import VerificationOutcome
from app.agents.graph import NODE_ORDER, build_graph
from app.models.enums import VerificationResult


def approval_reply(harness, approved=True):
    return Command(resume={"approvals": [{"action_id": harness["action_id"], "approved": approved}]})


def confirm_decisions(graph, config):
    snapshot = graph.get_state(config)
    assert snapshot.interrupts[0].value["kind"] == "confirm"
    return graph.invoke(Command(resume={"confirmations": [
        {"decision_id": value, "accepted": True} for value in snapshot.values["decision_ids"]
    ]}), config)


def test_missing_batch_interrupts_before_any_retrieval_and_survives_recompile(harness):
    h = harness
    h["medicine"]["batch_no"] = None
    first = h["graph"].invoke(h["input"], h["config"])
    assert first["__interrupt__"][0].value["kind"] == "collect"
    h["adapter"].fetch_notices.assert_not_called()
    rebuilt = build_graph(h["repository"], {"openFDA": h["adapter"]}, h["verifier"], h["saver"])
    paused = rebuilt.invoke(Command(resume={"batch_no": "SYN001"}), h["config"])
    assert paused["__interrupt__"][0].value["kind"] == "confirm"
    paused = confirm_decisions(rebuilt, h["config"])
    assert paused["__interrupt__"][0].value["kind"] == "approve"
    h["repository"].send_mock.assert_not_called()
    result = rebuilt.invoke(approval_reply(h), h["config"])
    assert result["completed_nodes"] == list(NODE_ORDER)
    assert result["plan"]["match_scope"] == ["brand", "strength", "batch"]
    assert result["outcome"] == "VERIFIED"
    assert h["repository"].save_decision.call_count == 1
    assert h["repository"].send_mock.call_count == 1
    h["repository"].save_verification.assert_called_once()


def test_invalid_batch_and_approval_replies_interrupt_again(harness):
    h = harness
    h["medicine"]["batch_no"] = None
    h["graph"].invoke(h["input"], h["config"])
    again = h["graph"].invoke(Command(resume={"batch_no": " "}), h["config"])
    assert again["__interrupt__"][0].value["error"]
    h["adapter"].fetch_notices.assert_not_called()
    h["graph"].invoke(Command(resume={"batch_no": "SYN001"}), h["config"])
    confirm_decisions(h["graph"], h["config"])
    again = h["graph"].invoke(approval_reply(h, approved="false"), h["config"])
    assert again["__interrupt__"][0].value["kind"] == "approve"
    assert again["__interrupt__"][0].value["error"]
    h["repository"].send_mock.assert_not_called()
    final = h["graph"].invoke(approval_reply(h, approved=False), h["config"])
    assert final["outcome"] == "NO_ALERTS"
    assert final["sent_action_ids"] == []
    h["verifier"].verify.assert_not_called()


@pytest.mark.parametrize("batch", ["OTHER001", "SYN00"])
def test_nonmatches_never_request_approval_or_send(harness, batch):
    h = harness
    h["medicine"]["batch_no"] = batch
    result = h["graph"].invoke(h["input"], h["config"])
    result = confirm_decisions(h["graph"], h["config"])
    assert "__interrupt__" not in result
    assert result["outcome"] == "NO_ALERTS"
    h["repository"].propose.assert_not_called()
    h["repository"].send_mock.assert_not_called()


def test_verification_failure_stays_in_checkpoint_and_db_call(harness):
    h = harness
    h["verifier"].verify.return_value = VerificationOutcome(
        None, None, None, VerificationResult.UNCERTAIN, "Synthetic timeout",
    )
    h["graph"].invoke(h["input"], h["config"])
    confirm_decisions(h["graph"], h["config"])
    result = h["graph"].invoke(approval_reply(h), h["config"])
    assert result["outcome"] == "FAILED"
    assert result["failures"][0]["reason"] == "Synthetic timeout"
    assert result["verification_results"][0]["result"] == "UNCERTAIN"
    assert h["graph"].get_state(h["config"]).values["failures"] == result["failures"]
    h["repository"].save_verification.assert_called_once()


def test_retrieval_errors_are_not_converted_to_no_matches(harness):
    from app.sources import SourceError
    h = harness
    h["adapter"].fetch_notices.side_effect = SourceError("Synthetic source unavailable")
    with pytest.raises(SourceError):
        h["graph"].invoke(h["input"], h["config"])
    assert h["graph"].get_state(h["config"]).next == ("retrieve",)
    h["repository"].send_mock.assert_not_called()
