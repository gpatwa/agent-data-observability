from agent_data_observability.snowflake_ import trace_tag


def test_trace_tag_strips_quotes_and_backslashes_from_model_authored_intent():
    tag = trace_tag({
        "trace_id": "abc", "span_id": "def", "parent_span_id": None,
        "agent_id": "a", "speculation_class": "probe",
        "span_intent": "it's a \\ backslash '; drop table orders; --",
    })
    assert "'" not in tag
    assert "\\" not in tag
    assert '"t":"abc"' in tag
    assert len(tag) <= 2000
