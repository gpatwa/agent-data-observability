"""Tests for the load-bearing logic: normalization, subsumption, and candidate
synthesis. Every redundancy number this repo reports comes out of these
functions, so they need golden cases — especially the NEGATIVE ones, where a
too-eager subsumption rule would silently inflate the headline result.

Ported 1:1 from test/fingerprint.test.mjs (the JS-era suite), with sqlglot's
own rendering substituted where the old regex-parser's exact string spacing
was an implementation detail rather than an invariant (noted inline).
"""

from agent_data_observability.shape import (
    ast_hash, exact_hash, extract_shape as shape, subsumes, covering_set, synthesize_candidates,
)


# --- normalization -----------------------------------------------------------

def test_alias_choice_does_not_change_the_hash():
    a = "select sum(amount) from orders o where o.region = 'EMEA'"
    b = "select sum(amount) from orders x where x.region = 'EMEA'"
    assert ast_hash(a) == ast_hash(b)
    assert exact_hash(a) != exact_hash(b), "exact hash should still differ"


def test_predicate_order_does_not_change_the_hash():
    a = "select sum(amount) from orders where region = 'EMEA' and channel = 'email'"
    b = "select sum(amount) from orders where channel = 'email' and region = 'EMEA'"
    assert ast_hash(a) == ast_hash(b)


def test_whitespace_and_case_do_not_change_the_hash():
    a = "SELECT   sum(amount)\n  FROM orders\n WHERE region = 'EMEA'"
    b = "select sum(amount) from orders where region = 'EMEA'"
    assert ast_hash(a) == ast_hash(b)


def test_a_genuinely_different_query_gets_a_different_hash():
    a = "select sum(amount) from orders where region = 'EMEA'"
    b = "select sum(amount) from orders where region = 'APAC'"
    assert ast_hash(a) != ast_hash(b)


# --- subsumption: positive cases ---------------------------------------------

def test_a_rollup_subsumes_a_query_filtered_on_the_grouped_column():
    rollup = shape("select order_date, sum(amount) from orders group by order_date")
    daily = shape("select sum(amount) from orders where order_date = '2026-07-12'")
    assert subsumes(rollup, daily)


def test_a_two_dimension_rollup_subsumes_a_single_cell_query():
    cube = shape("select region, channel, sum(amount) from orders group by region, channel")
    cell = shape("select sum(amount) from orders where region = 'EMEA' and channel = 'email'")
    assert subsumes(cube, cell)


def test_avg_is_servable_from_sum_plus_count():
    rollup = shape("select region, sum(amount), count(*) from orders group by region")
    avg_q = shape("select avg(amount) from orders where region = 'EMEA'")
    assert subsumes(rollup, avg_q)


def test_a_coarser_grouping_subsumes_a_finer_one_on_the_same_measures():
    fine = shape("select region, channel, sum(amount) from orders group by region, channel")
    coarse = shape("select region, sum(amount) from orders group by region")
    assert subsumes(fine, coarse), "region+channel rollup should answer region-only"


# --- subsumption: negative cases (the ones that keep the numbers honest) ----

def test_does_not_subsume_when_the_filter_column_is_not_in_the_grouping():
    rollup = shape("select region, sum(amount) from orders group by region")
    q = shape("select sum(amount) from orders where channel = 'email'")
    assert subsumes(rollup, q) is False


def test_does_not_subsume_when_the_anchor_is_more_restrictive_than_the_query():
    narrow = shape("select order_date, sum(amount) from orders where region = 'EMEA' group by order_date")
    wide = shape("select sum(amount) from orders where order_date = '2026-07-12'")
    assert subsumes(narrow, wide) is False


def test_does_not_subsume_across_different_tables():
    orders = shape("select order_date, sum(amount) from orders group by order_date")
    refunds = shape("select sum(amount) from refunds where refund_date = '2026-07-12'")
    assert subsumes(orders, refunds) is False


def test_does_not_subsume_a_measure_the_anchor_never_computed():
    rollup = shape("select region, sum(amount) from orders group by region")
    q = shape("select count(order_id) from orders where region = 'EMEA'")
    assert subsumes(rollup, q) is False


def test_avg_alone_cannot_serve_another_avg_at_finer_grain():
    rollup = shape("select region, avg(amount) from orders group by region")
    q = shape("select avg(amount) from orders where region = 'EMEA' and channel = 'email'")
    assert subsumes(rollup, q) is False, "avg is not additive and channel is not grouped"


def test_a_finer_grouping_cannot_be_recovered_from_a_coarser_one():
    coarse = shape("select region, sum(amount) from orders group by region")
    fine = shape("select region, channel, sum(amount) from orders group by region, channel")
    assert subsumes(coarse, fine) is False


def test_non_aggregate_queries_do_not_participate():
    assert shape("select * from orders limit 5") is None
    assert shape("select order_id from orders where region = 'EMEA'") is None


# --- candidate synthesis ------------------------------------------------------

def test_synthesis_lifts_equality_filters_into_the_grouping():
    shapes = [
        shape("select sum(amount) from orders where order_date = '2026-07-01'"),
        shape("select sum(amount) from orders where order_date = '2026-07-02'"),
    ]
    cands = synthesize_candidates(shapes)
    assert any("order_date" in c.groupby and not c.filters for c in cands), \
        "expected a synthesized GROUP BY order_date candidate"


def test_one_synthesized_anchor_covers_a_whole_per_day_fan_out():
    shapes = [
        shape(f"select count(order_id), sum(amount) from orders where order_date = '2026-07-{i+1:02d}'")
        for i in range(31)
    ]
    cover = covering_set(shapes)
    assert cover.covered_count == 31
    assert len(cover.anchors) == 1, "a single GROUP BY order_date should serve all 31"
    assert cover.anchors[0]["anchor"].synthetic, "and it should be synthesized, not observed"


def test_unrelated_queries_are_not_collapsed_together():
    shapes = [
        shape("select sum(amount) from orders where region = 'EMEA'"),
        shape("select sum(amount) from refunds where refund_date = '2026-07-01'"),
    ]
    cover = covering_set(shapes)
    assert len(cover.anchors) >= 2, "different tables cannot share one anchor"


def test_covering_set_never_claims_to_cover_a_query_it_cannot():
    shapes = [
        shape("select region, sum(amount) from orders group by region"),
        shape("select channel, avg(amount) from orders group by channel"),
        shape("select sum(amount) from refunds"),
    ]
    cover = covering_set(shapes)
    for a in cover.anchors:
        for i in a["covers"]:
            assert subsumes(a["anchor"], shapes[i]), \
                f"anchor claimed to cover query {i} but does not subsume it"


# --- real-agent SQL patterns the OLD regex extractor could not read ---------

def test_positional_group_by_resolves_to_the_select_list_expressions():
    s = shape("select region, channel, sum(amount) from orders group by 1, 2")
    assert s.groupby == ("channel", "region")
    assert s.measures == ("sum(amount)",)


def test_an_alias_in_group_by_resolves_to_the_underlying_expression():
    s = shape("select date_trunc('month', order_date) as m, sum(amount) from orders group by m")
    # sqlglot renders the function call with a space after the comma — a
    # cosmetic difference from the old parser's regex output, not a
    # correctness difference. What matters: it resolved through the alias to
    # the underlying expression rather than the literal string "m".
    assert s.groupby == ("date_trunc('month', order_date)",)


def test_a_daily_rollup_subsumes_a_monthly_question_days_roll_up():
    daily = shape("select order_date, sum(amount) from orders group by order_date")
    monthly = shape("select date_trunc('month', order_date), sum(amount) from orders group by 1")
    assert subsumes(daily, monthly)


def test_a_monthly_rollup_does_not_subsume_a_daily_question():
    monthly = shape("select date_trunc('month', order_date), sum(amount) from orders group by 1")
    daily = shape("select order_date, sum(amount) from orders group by order_date")
    assert subsumes(monthly, daily) is False


def test_count_distinct_is_not_derivable_from_a_grouped_rollup():
    rollup = shape("select region, count(distinct customer_id) from orders group by region")
    q = shape("select count(distinct customer_id) from orders where region = 'EMEA'")
    assert subsumes(rollup, q) is False, "distinct counts do not sum"


def test_queries_with_joins_are_excluded_rather_than_misparsed():
    assert shape(
        "select sum(o.amount) from orders o join refunds r on r.order_id = o.order_id group by o.region"
    ) is None


def test_ctes_are_excluded():
    assert shape("with x as (select * from orders) select sum(amount) from x") is None


def test_or_in_the_where_clause_is_excluded():
    assert shape("select sum(amount) from orders where region = 'EMEA' or region = 'APAC'") is None


def test_having_is_excluded():
    assert shape("select region, sum(amount) from orders group by region having sum(amount) > 100") is None


def test_unparseable_sql_returns_none_instead_of_raising():
    assert shape("this is not sql at all") is None
    assert shape("") is None


def test_a_cast_dimension_is_distinct_from_its_base_column():
    s = shape("select order_date::date, sum(amount) from orders group by 1")
    assert len(s.groupby) == 1
    assert s.groupby[0] != "order_date"


# --- false positives: shapes that LOOKED modelled but described the wrong query

def test_window_functions_are_declined_not_modelled():
    assert shape("select region, sum(amount) over (partition by region) from orders") is None


def test_a_subquery_inside_where_is_declined():
    assert shape("select sum(amount) from orders where customer_id in (select id from customers)") is None


def test_a_subquery_in_from_is_declined():
    assert shape("select sum(s) from (select sum(amount) s from orders group by region) t") is None


def test_multiple_statements_are_never_modelled():
    assert shape("select 1; drop table orders") is None
