from agent_data_observability.readonly import read_only_refusal


def test_read_only_guard_rejects_a_trailing_drop_smuggled_after_a_select():
    assert read_only_refusal("select 1; drop table orders")
    assert read_only_refusal("drop table orders")
    assert read_only_refusal("update orders set amount = 0")
    assert read_only_refusal("  ")
    assert read_only_refusal("select sum(amount) from orders") is None
