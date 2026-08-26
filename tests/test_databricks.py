from agent_data_observability.databricks import extract_attachments, rows_from_result
from agent_data_observability.shape import extract_shape


def test_rows_from_result_normalises_statement_execution_shape_to_objects():
    result = {
        "statement_response": {
            "manifest": {"schema": {"columns": [{"name": "nation"}, {"name": "revenue"}]}},
            "result": {"data_array": [["FRANCE", "8960000.50"], ["CANADA", "8470000.25"]]},
        },
    }
    assert rows_from_result(result) == [
        {"nation": "FRANCE", "revenue": "8960000.50"},
        {"nation": "CANADA", "revenue": "8470000.25"},
    ]


def test_rows_from_result_returns_empty_rather_than_throwing_on_an_unexpected_shape():
    assert rows_from_result(None) == []
    assert rows_from_result({}) == []
    assert rows_from_result({"statement_response": {"result": {"data_array": [[1]]}}}) == []


def test_extract_attachments_finds_the_generated_sql_and_the_attachment_id():
    msg = {
        "attachments": [
            {"text": {"content": "France leads narrowly."}},
            {
                "attachment_id": "att_123",
                "query": {"query": "SELECT n_name, sum(l_extendedprice) FROM ...", "description": "revenue by nation"},
            },
        ],
    }
    a = extract_attachments(msg)
    assert a["attachmentId"] == "att_123"
    assert a["sql"].startswith("SELECT n_name")
    assert a["text"] == "France leads narrowly."
    assert a["description"] == "revenue by nation"


def test_extract_attachments_tolerates_a_narrative_only_answer_with_no_query():
    a = extract_attachments({"attachments": [{"text": {"content": "I need more detail."}}]})
    assert a["sql"] is None
    assert a["attachmentId"] is None
    assert a["text"] == "I need more detail."


def test_extract_attachments_handles_a_message_with_no_attachments_at_all():
    assert extract_attachments({}) == {"text": None, "sql": None, "attachmentId": None, "description": None}


def test_genie_generated_sql_still_feeds_the_shape_extractor():
    a = extract_attachments({
        "attachments": [{
            "attachment_id": "a1",
            "query": {"query": "select region, sum(amount) from orders where order_date >= '2026-07-01' group by 1"},
        }],
    })
    s = extract_shape(a["sql"])
    assert s is not None, "Genie SQL must be modellable — this is what preserves sub-expression analysis"
    assert list(s.groupby) == ["region"]
    assert list(s.measures) == ["sum(amount)"]
