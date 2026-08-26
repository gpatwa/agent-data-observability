from pathlib import Path

from agent_data_observability.trace import bill, parse_log, reconstruct

FIXTURE = Path(__file__).resolve().parent.parent / "docs" / "runs" / "same-task-queries.log"


def test_reconstruct_recovers_the_same_task_replication_spans():
    """Regression check against the real saved log the README's headline
    numbers (54/55, 51/55, 17.7%) were computed from."""
    spans = reconstruct(str(FIXTURE), [])
    assert len(spans) == 55
    assert all(s["sql"] and s["exact"] and s["ast"] and s["trace_id"] for s in spans)
    assert spans == sorted(spans, key=lambda s: s["start_ms"])


def test_bill_charges_at_least_the_minimum_window_and_never_loses_time():
    spans = reconstruct(str(FIXTURE), [])
    b = bill(spans)
    assert b["billedSec"] >= b["productiveSec"] > 0
    assert b["elapsedSec"] > 0


def test_parse_log_pairs_some_statements_with_a_duration():
    # `cur` tracks one in-flight statement globally, not per-PID, so with
    # interleaved concurrent sessions a statement can be superseded by the
    # next one before its own duration line arrives and is pushed without
    # duration_ms. This mirrors the original parser's exact behavior.
    events = parse_log(str(FIXTURE))
    assert len(events) > 0
    assert any("duration_ms" in e for e in events)
