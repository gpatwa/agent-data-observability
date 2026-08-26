from agent_data_observability.verify_citations import parse_cited


def test_cited_line_is_parsed_through_markdown_emphasis():
    assert parse_cited("text\n**CITED: q1, q4, q5**") == {"q1", "q4", "q5"}
    assert parse_cited("CITED: q1,q2") == {"q1", "q2"}
    assert parse_cited("  - CITED: q3 ") == {"q3"}
    assert parse_cited("no citation line here") is None
