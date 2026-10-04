import datetime

from agent_data_observability.question_cost import build

UTC = datetime.timezone.utc
TRACE = "a" * 32
ROOT = "b" * 16


def _run():
    return {
        "trace_id": TRACE, "root_span_id": ROOT, "tag": "t1", "question": "q?",
        "agent_id": "claude-code-t1", "started_unix_ms": 1_000_000.0, "ended_unix_ms": 1_060_000.0,
        "llm": {"cost_usd": 0.25, "num_turns": 4,
                "usage": {"input_tokens": 10, "cache_creation_input_tokens": 100,
                          "cache_read_input_tokens": 1000, "output_tokens": 50},
                "model_usage": {"claude-opus-5": {}}},
    }


def _event(label, span_id, qid, parent=ROOT):
    return {"label": label, "span_id": span_id, "parent_span_id": parent, "snowflake_query_id": qid,
            "span_intent": f"intent {label}", "grounded": True, "started_unix_ms": 1_010_000.0, "client_ms": 500}


def _wh(qid):
    return {"QUERY_ID": qid, "QUERY_TEXT": "select 1", "START_TIME": datetime.datetime(2026, 1, 1, tzinfo=UTC),
            "END_TIME": datetime.datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC), "EXECUTION_TIME": 800,
            "BYTES_SCANNED": 1024, "WAREHOUSE_NAME": "COMPUTE_WH", "WAREHOUSE_SIZE": "X-Small"}


def test_credits_pending_leaves_total_unset_rather_than_estimating():
    s = build(_run(), [_event("q1", "c" * 16, "Q1")], [_wh("Q1")], {}, 3.0)
    assert s["credits_status"] == "pending"
    assert s["total_cost_usd"] is None
    assert s["llm_cost_usd"] == 0.25
    assert s["input_tokens"] == 1110


def test_measured_credits_produce_a_total_of_llm_plus_warehouse():
    events = [_event("q1", "c" * 16, "Q1"), _event("q2", "d" * 16, "Q2", parent="c" * 16)]
    s = build(_run(), events, [_wh("Q1"), _wh("Q2")], {"Q1": 0.01, "Q2": 0.02}, 3.0)
    assert s["credits_status"] == "measured"
    assert abs(s["warehouse_cost_usd"] - 0.09) < 1e-9
    assert abs(s["total_cost_usd"] - 0.34) < 1e-9


def test_partial_credits_are_reported_as_partial_not_measured():
    events = [_event("q1", "c" * 16, "Q1"), _event("q2", "d" * 16, "Q2")]
    s = build(_run(), events, [_wh("Q1"), _wh("Q2")], {"Q1": 0.01}, 3.0)
    assert s["credits_status"] == "partial"
    assert s["total_cost_usd"] is None


def test_disagreement_between_agent_and_warehouse_is_shown_not_dropped():
    events = [_event("q1", "c" * 16, "Q1"), _event("q2", "d" * 16, "Q-AGENT-ONLY")]
    s = build(_run(), events, [_wh("Q1"), _wh("Q-WH-ONLY")], {}, 3.0)
    seen = {q["query_id"]: q["seen_by"] for q in s["queries"]}
    assert seen == {"Q1": "both", "Q-WH-ONLY": "warehouse", "Q-AGENT-ONLY": "agent"}
    assert s["agent_only"] == 1 and s["warehouse_only"] == 1


def test_otlp_spans_form_one_tree_under_the_agent_run():
    events = [_event("q1", "c" * 16, "Q1"), _event("q2", "d" * 16, "Q2", parent="c" * 16)]
    s = build(_run(), events, [_wh("Q1"), _wh("Q2")], {}, 3.0)
    spans = s["otlp"]["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert {sp["traceId"] for sp in spans} == {TRACE}
    root = [sp for sp in spans if "parentSpanId" not in sp]
    assert len(root) == 1 and root[0]["spanId"] == ROOT
    parents = {sp["spanId"]: sp.get("parentSpanId") for sp in spans}
    assert parents["c" * 16] == ROOT and parents["d" * 16] == "c" * 16
    assert all(len(sp["spanId"]) == 16 for sp in spans)
    assert all(sp["startTimeUnixNano"].isdigit() for sp in spans)
