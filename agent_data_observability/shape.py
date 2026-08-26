"""Query shape extraction, subsumption and candidate synthesis — via sqlglot.

WHY THIS REPLACES THE HAND-ROLLED (then node-sql-parser) VERSIONS:
The original JS parser (src/shape.mjs, node-sql-parser) modelled roughly 1 in 4
real analytics queries — joins, CTEs, subqueries, window functions were all
declined for lack of AST support deep enough to trust. sqlglot is a real,
multi-dialect SQL engine with a proper expression tree, so the same class of
query is either handled correctly or declined on purpose, not by accident.

DESIGN RULE (unchanged from the JS version): when this cannot confidently
model a query it returns None and the query is excluded from analysis. An
excluded query lowers reported coverage; a mis-parsed one inflates it.
Excluding is the honest failure. Joins, correlated subqueries, window
functions, CTEs, HAVING, SELECT DISTINCT and top-level OR are all declined.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from typing import Optional

import sqlglot
from sqlglot import exp

DIALECT = "postgres"

# Functions that bucket a column into a coarser grain. A rollup grouped by the
# underlying column can serve a query grouped by the bucket (days roll up into
# months); the reverse is never true.
BUCKETING = {"date_trunc", "date", "to_char", "extract", "date_part"}


# ---------------------------------------------------------------------------
# Hashing (exact / AST-normalized) — kept separate from shape extraction on
# purpose. These only need to decide whether two queries are textually the
# same modulo cosmetics; they are measured at a 0-2% hit rate in every
# real-world run so far. Everything that feeds a redundancy NUMBER goes
# through extract_shape() below instead.

def exact_hash(sql: str) -> str:
    return hashlib.sha1(sql.strip().encode()).hexdigest()[:12]


def ast_hash(sql: str) -> str:
    """Hash of a canonical rendering that is insensitive to alias choice,
    predicate order, and whitespace/case.

    sqlglot's own ``normalize=True`` does NOT unify table aliases or reorder
    AND-conjuncts (verified empirically — it only canonicalizes casing and
    spacing), so both are done explicitly here via the AST rather than regex.
    ``_canon``/``_split_conjuncts`` are defined later in this module; Python
    resolves the reference at call time, after the whole module has loaded.
    """
    try:
        tree = sqlglot.parse_one(sql, dialect=DIALECT)
    except Exception:
        return exact_hash(sql)
    if not isinstance(tree, exp.Select):
        return hashlib.sha1(tree.sql(dialect=DIALECT, normalize=True).lower().encode()).hexdigest()[:12]

    working = tree.copy()

    # Map every FROM/JOIN alias to its real table name, then rewrite column
    # qualifiers through that map — two queries that alias the same table
    # differently should hash identically.
    alias_map: dict[str, str] = {}
    from_ = working.args.get("from_")
    if from_ is not None and isinstance(from_.this, exp.Table) and from_.this.alias:
        alias_map[from_.this.alias.lower()] = from_.this.name.lower()
    for j in working.find_all(exp.Join):
        if isinstance(j.this, exp.Table) and j.this.alias:
            alias_map[j.this.alias.lower()] = j.this.name.lower()
    for col in working.find_all(exp.Column):
        if col.table and col.table.lower() in alias_map:
            col.set("table", exp.to_identifier(alias_map[col.table.lower()]))

    select_part = ", ".join(sorted(_canon(e) for e in working.selects))

    from_part = ""
    if from_ is not None:
        from_part = (from_.this.name.lower() if isinstance(from_.this, exp.Table)
                     else _canon(from_.this))

    where = working.args.get("where")
    where_part = ""
    if where is not None:
        conjuncts = _split_conjuncts(where.this)
        # OR present: fall back to the whole predicate, unsorted (still
        # canonical, just not reorder-insensitive — matches the source query
        # having no top-level conjuncts to reorder in the first place).
        where_part = (" and ".join(sorted(_canon(c) for c in conjuncts))
                      if conjuncts is not None else _canon(where.this))

    group = working.args.get("group")
    group_part = ", ".join(sorted(_canon(e) for e in group.expressions)) if group else ""

    canonical = f"select {select_part} from {from_part}"
    if where_part:
        canonical += f" where {where_part}"
    if group_part:
        canonical += f" group by {group_part}"
    return hashlib.sha1(canonical.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Shape

@dataclass(frozen=True)
class QueryShape:
    table: str
    measures: tuple[str, ...]
    groupby: tuple[str, ...]
    filters: tuple[str, ...]
    eq_cols: tuple[str, ...]
    synthetic: bool = False
    sql: Optional[str] = None


def _canon(node: exp.Expression) -> str:
    """Canonical text for an expression: lowercase, table-qualifiers stripped
    from columns. Used for filters, measures and non-column dimensions."""
    stripped = node.copy()
    for col in stripped.find_all(exp.Column):
        col.set("table", None)
    return stripped.sql(dialect=DIALECT).lower()


def _measure_key(agg: exp.AggFunc) -> str:
    if isinstance(agg, exp.Count) and isinstance(agg.this, exp.Distinct):
        return _canon(agg)
    return _canon(agg)


def _split_conjuncts(node: Optional[exp.Expression]) -> Optional[list[exp.Expression]]:
    """Top-level AND conjuncts. Returns None if an OR appears anywhere in the
    walk (disjunction breaks the subsumption reasoning entirely — see the
    module docstring)."""
    if node is None:
        return []
    if isinstance(node, exp.Or):
        return None
    if isinstance(node, exp.And):
        left = _split_conjuncts(node.left)
        if left is None:
            return None
        right = _split_conjuncts(node.right)
        if right is None:
            return None
        return left + right
    if isinstance(node, exp.Paren):
        return _split_conjuncts(node.this)
    return [node]


def _has_unmodellable_construct(tree: exp.Select) -> bool:
    if tree.args.get("with") or tree.args.get("having") or tree.args.get("qualify"):
        return True
    if tree.args.get("distinct"):
        return True
    if list(tree.find_all(exp.Join)):
        return True
    if list(tree.find_all(exp.Window)):
        return True
    # Any nested SELECT anywhere (WHERE/FROM/select-list subqueries) — walk
    # everything under the root and flag a second Select at any depth.
    for node in tree.walk():
        n = node[0] if isinstance(node, tuple) else node
        if isinstance(n, exp.Select) and n is not tree:
            return True
    return False


def extract_shape(sql: str) -> Optional[QueryShape]:
    try:
        statements = sqlglot.parse(sql, dialect=DIALECT)
    except Exception:
        return None
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        return None  # multiple statements is not an analytics query
    tree = statements[0]
    if not isinstance(tree, exp.Select):
        return None
    if _has_unmodellable_construct(tree):
        return None

    from_ = tree.args.get("from_")
    if not from_ or not isinstance(from_.this, exp.Table):
        return None
    table = from_.this.name.lower()

    # --- select list: measures vs dimensions --------------------------------
    measures: list[str] = []
    select_keys: list[Optional[str]] = []  # positional GROUP BY resolves here
    alias_to_key: dict[str, str] = {}

    for e in tree.selects:
        if isinstance(e, exp.Star):
            return None
        inner = e.this if isinstance(e, exp.Alias) else e
        if isinstance(inner, exp.AggFunc):
            measures.append(_measure_key(inner))
            select_keys.append(None)  # aggregates are not groupable positions
        else:
            key = inner.name if isinstance(inner, exp.Column) else _canon(inner)
            select_keys.append(key)
            if isinstance(e, exp.Alias):
                alias_to_key[e.alias.lower()] = key

    if not measures:
        return None  # only aggregate queries participate

    # --- group by -------------------------------------------------------------
    groupby: list[str] = []
    group = tree.args.get("group")
    for g in (group.expressions if group else []):
        if isinstance(g, exp.Literal) and not g.is_string:
            idx = int(g.this) - 1
            if idx < 0 or idx >= len(select_keys) or select_keys[idx] is None:
                return None  # positional ref to an aggregate, or out of range
            groupby.append(select_keys[idx])
            continue
        key = g.name if isinstance(g, exp.Column) else _canon(g)
        key = alias_to_key.get(key, key)
        groupby.append(key)

    # --- where ------------------------------------------------------------
    where = tree.args.get("where")
    conjuncts = _split_conjuncts(where.this if where else None)
    if conjuncts is None:
        return None  # OR present

    filters: list[str] = []
    eq_cols: list[str] = []
    for c in conjuncts:
        filters.append(_canon(c))
        if isinstance(c, exp.EQ) and isinstance(c.left, exp.Column):
            eq_cols.append(c.left.name.lower())

    return QueryShape(
        table=table,
        measures=tuple(sorted(set(measures))),
        groupby=tuple(sorted(set(groupby))),
        filters=tuple(sorted(set(filters))),
        eq_cols=tuple(sorted(set(eq_cols))),
    )


# ---------------------------------------------------------------------------
# Subsumption

def _measure_servable(b: QueryShape, m: str) -> bool:
    if m.startswith("count(distinct "):
        # Distinct counts do not sum — a rollup can serve this only if it
        # already computed the exact same distinct count with no finer grain.
        return m in b.measures and len(b.groupby) == 0
    avg_arg = None
    if m.startswith("avg(") and m.endswith(")"):
        avg_arg = m[4:-1]
    if avg_arg is not None:
        has_sum = f"sum({avg_arg})" in b.measures
        has_count = any(x.startswith("count(") and "distinct" not in x for x in b.measures)
        return has_sum and has_count
    return m in b.measures  # sum/count/min/max


def _dimension_derivable(b: QueryShape, d: str) -> bool:
    if d in b.groupby:
        return True
    if "(" not in d or not d.endswith(")"):
        return False
    fn, _, rest = d.partition("(")
    if fn not in BUCKETING:
        return False
    inner_cols = [c.strip().strip("'\"") for c in rest[:-1].split(",")]
    return any(col in b.groupby for col in inner_cols)


def subsumes(b: QueryShape, a: Optional[QueryShape]) -> bool:
    """Can query `a` be answered from anchor `b`'s result set?"""
    if a is None or b is None:
        return False
    if a.table != b.table:
        return False
    if not all(_measure_servable(b, m) for m in a.measures):
        return False
    if not all(_dimension_derivable(b, g) for g in a.groupby):
        return False
    # b must be no more restrictive than a: every filter b applies, a must too.
    if not all(f in a.filters for f in b.filters):
        return False
    # Any extra restriction a has beyond b must be selectable out of b's
    # result, which requires b to have grouped by that column.
    extra = [f for f in a.filters if f not in b.filters]
    for f in extra:
        if "=" not in f:
            return False
        col = f.split("=", 1)[0].strip()
        if not col.isidentifier() or col not in b.groupby:
            return False
    return True


# ---------------------------------------------------------------------------
# Candidate synthesis

def _render_sql(s: QueryShape) -> str:
    dims = ", ".join(s.groupby)
    sel = ", ".join(p for p in (dims, ", ".join(s.measures)) if p)
    where = f" where {' and '.join(s.filters)}" if s.filters else ""
    grp = f" group by {dims}" if s.groupby else ""
    return f"select {sel} from {s.table}{where}{grp}"


def synthesize_candidates(shapes: list[Optional[QueryShape]]) -> list[QueryShape]:
    by_table: dict[str, list[QueryShape]] = {}
    for s in shapes:
        if s is None:
            continue
        by_table.setdefault(s.table, []).append(s)

    out: list[QueryShape] = []
    for table, group in by_table.items():
        measures = {"count(*)"}
        for s in group:
            for m in s.measures:
                if m.startswith("count(distinct "):
                    continue  # not derivable; don't promise it
                if m.startswith("avg(") and m.endswith(")"):
                    measures.add(f"sum({m[4:-1]})")
                else:
                    measures.add(m)

        dim_sets: dict[str, tuple[str, ...]] = {}
        for s in group:
            dims = tuple(sorted(set(s.groupby) | set(s.eq_cols)))
            dim_sets["|".join(dims)] = dims

        for dims in dim_sets.values():
            out.append(QueryShape(table=table, measures=tuple(sorted(measures)),
                                   groupby=dims, filters=(), eq_cols=(), synthetic=True))

        all_dims = tuple(sorted({d for dims in dim_sets.values() for d in dims}))
        if all_dims:
            out.append(QueryShape(table=table, measures=tuple(sorted(measures)),
                                   groupby=all_dims, filters=(), eq_cols=(), synthetic=True))

    return [replace(c, sql=_render_sql(c)) for c in out]


@dataclass
class CoveringSetResult:
    anchors: list[dict]  # [{"anchor": QueryShape, "covers": [idx, ...]}]
    covered_count: int
    total: int
    unmodelled: int
    uncovered: list[int]


def covering_set(shapes: list[Optional[QueryShape]]) -> CoveringSetResult:
    """Greedy set cover: the smallest set of anchor queries whose results
    could answer every aggregate query in the workload."""
    idxs = [i for i, s in enumerate(shapes) if s is not None]
    pool: list[QueryShape] = synthesize_candidates(shapes)
    pool += [replace(shapes[i], synthetic=False, sql=_render_sql(shapes[i])) for i in idxs]

    covered: set[int] = set()
    anchors: list[dict] = []
    while len(covered) < len(idxs):
        best: Optional[QueryShape] = None
        best_cover: list[int] = []
        for cand in pool:
            cov = [j for j in idxs if j not in covered and subsumes(cand, shapes[j])]
            if len(cov) > len(best_cover):
                best, best_cover = cand, cov
        if best is None or not best_cover:
            break
        anchors.append({"anchor": best, "covers": best_cover})
        covered.update(best_cover)

    return CoveringSetResult(
        anchors=anchors,
        covered_count=len(covered),
        total=len(idxs),
        unmodelled=len(shapes) - len(idxs),
        uncovered=[i for i in idxs if i not in covered],
    )
