"""Checkpoint resume/replay behavior using a real graph and in-memory saver."""

from contextlib import contextmanager
from uuid import uuid4

from app.services.run_worker import GraphRunner


def runner_for(harness):
    @contextmanager
    def factory():
        yield harness["graph"]
    return GraphRunner(factory)


def test_consumed_batch_reply_cannot_approve_next_question(harness):
    h = harness
    h["medicine"]["batch_no"] = None
    runner, run_id = runner_for(h), uuid4()
    med_id, destination = h["input"]["medicine_id"], h["input"]["destination"]
    first = runner.step(run_id, med_id, destination, None)
    question = first.pending_interrupt
    assert question["value"]["kind"] == "collect"
    reply = {"thread_id": question["thread_id"], "interrupt_id": question["id"],
             "value": {"batch_no": "SYN001"}}
    second = runner.step(run_id, med_id, destination, reply)
    assert second.pending_interrupt["value"]["kind"] == "confirm"
    # Simulate a crash after LangGraph saved, before the runs row was updated.
    recovered = runner_for(h).step(run_id, med_id, destination, reply)
    assert recovered.pending_interrupt == second.pending_interrupt
    h["repository"].approve.assert_not_called()
    h["repository"].send_mock.assert_not_called()
    question = recovered.pending_interrupt
    confirm_reply = {"thread_id": question["thread_id"], "interrupt_id": question["id"],
                     "value": {"confirmations": [
                         {"decision_id": item["decision_id"], "accepted": True}
                         for item in question["value"]["decisions"]]}}
    confirmed = runner.step(run_id, med_id, destination, confirm_reply)
    assert confirmed.pending_interrupt["value"]["kind"] == "approve"
    assert runner.step(run_id, med_id, destination, confirm_reply).pending_interrupt == confirmed.pending_interrupt
    question = confirmed.pending_interrupt
    reply = {"thread_id": question["thread_id"], "interrupt_id": question["id"],
             "value": {"approvals": [{"action_id": h["action_id"], "approved": True}]}}
    final = runner.step(run_id, med_id, destination, reply)
    assert final.state["outcome"] == "VERIFIED" and final.pending_interrupt is None
    replay = runner_for(h).step(run_id, med_id, destination, reply)
    assert replay.state == final.state
    h["repository"].send_mock.assert_called_once()


def test_infrastructure_failure_retains_partial_graph_state(harness):
    harness["adapter"].fetch_notices.side_effect = RuntimeError("synthetic secret must not be exposed")
    result = runner_for(harness).step(uuid4(), harness["input"]["medicine_id"], "mock://test", None)
    assert result.error == "Agent execution failed (RuntimeError)."
    assert result.state["completed_nodes"] == ["collect", "plan"]
    assert result.pending_interrupt is None


def test_live_progress_contains_real_node_completions_and_no_interrupt_objects(harness):
    progress = []
    result = runner_for(harness).step(uuid4(), harness["input"]["medicine_id"], "mock://test", None,
                                      on_progress=progress.append)
    assert result.pending_interrupt["value"]["kind"] == "confirm"
    completed = [item.get("completed_nodes", []) for item in progress]
    assert ["collect"] in completed
    assert ["collect", "plan"] in completed
    assert ["collect", "plan", "retrieve", "match"] in completed
    assert all("__interrupt__" not in item for item in progress)
    harness["repository"].send_mock.assert_not_called()
