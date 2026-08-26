"""Phase 0a — verify `used_downstream` instead of trusting it.

The agent self-reports which queries it cited ("CITED: q1, q4"). That is the
agent grading its own homework, and the "zero speculation waste" finding
rested on it. This module checks the claim against evidence: does a value
from that query's result set actually appear in the final answer?

Two strengths of evidence:
    grounded          - some value from this result appears in the answer
    uniquely_grounded - a value appears that NO other query's result contained

`uniquely_grounded` is the strong signal. `grounded` alone is weak: a value
like a row count of 6000 may appear in many result sets at once.
"""

from __future__ import annotations

import calendar
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Optional

MAGNITUDE = {"k": 1e3, "m": 1e6, "b": 1e9, "bn": 1e9, "t": 1e12}

_CITED_RE = re.compile(r"^[\s*_#>\-]*CITED:\s*(.+?)[\s*_]*$", re.MULTILINE | re.IGNORECASE)


def parse_cited(answer: str) -> Optional[set]:
    """The agent's own claim about which queries it used. Models wrap this
    line in markdown ("**CITED: q1, q4**"), so tolerate emphasis and list
    markers — a line-anchored ^CITED: silently scores such a run as citing
    nothing."""
    m = _CITED_RE.search(answer)
    if not m:
        return None
    parts = re.split(r"[,\s]+", m.group(1))
    return {re.sub(r"[^a-z0-9]", "", p.strip().lower()) for p in parts if p.strip()}


_NUM_RE = re.compile(r"(-?\d[\d,]*\.?\d*)\s*(k|m|b|bn|t)?\b", re.IGNORECASE)


def numbers_in_text(text: str) -> list[float]:
    """Pull numbers out of prose, expanding $319.9M -> 319900000 and
    stripping thousands separators. Percentages are kept as their literal
    number."""
    out = []
    for m in _NUM_RE.finditer(text):
        raw = m.group(1).replace(",", "")
        try:
            base = float(raw)
        except ValueError:
            continue
        out.append(base)
        suf = (m.group(2) or "").lower()
        if suf and suf in MAGNITUDE:
            out.append(base * MAGNITUDE[suf])
    return out


def _number_present(value: float, answer_numbers: list[float], tol: float = 0.02) -> bool:
    """A result value counts as present if the answer contains a number
    within tolerance. 2% absorbs the rounding agents do when they write
    prose ("$319.9M" for 319875432.11, "~$150" for 149.87)."""
    for a in answer_numbers:
        denom = max(abs(value), 1)
        if abs(a - value) / denom <= tol:
            return True
    return False


_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _string_present(value, answer_lower: str) -> bool:
    v = str(value).strip()
    if len(v) < 3:
        return False  # too short to be evidence
    if v.lower() in answer_lower:
        return True
    # ISO dates also appear written out: 2026-07-12 -> "July 12"
    m = _ISO_DATE_RE.match(v)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        month_name = calendar.month_name[month]
        if f"{month_name.lower()} {day}" in answer_lower:
            return True
    return False


_NUMERIC_STRING_RE = re.compile(r"^-?\d+(\.\d+)?$")


def _normalize_value(v):
    """Values captured before the numeric-string fix (and any future
    producer that hands us "123.45") must compare numerically, not as
    substrings."""
    if isinstance(v, str) and _NUMERIC_STRING_RE.match(v.strip()):
        return float(v)
    return v


def verify(events: list[dict], answer: str) -> list[dict]:
    answer_lower = answer.lower()
    answer_numbers = numbers_in_text(answer)

    # How many result sets contained each value? Values seen everywhere are
    # not evidence that any particular query reached the answer.
    value_freq: dict = {}
    for e in events:
        seen = {_normalize_value(v) for v in e.get("values") or []}
        for v in seen:
            value_freq[v] = value_freq.get(v, 0) + 1

    out = []
    for e in events:
        grounded = False
        uniquely = False
        hits = []
        for v in [_normalize_value(v) for v in (e.get("values") or [])]:
            present = _number_present(v, answer_numbers) if isinstance(v, (int, float)) \
                else _string_present(v, answer_lower)
            if not present:
                continue
            grounded = True
            if value_freq.get(v, 0) == 1:
                uniquely = True
                if len(hits) < 4:
                    hits.append(v)
            elif len(hits) < 4:
                hits.append(v)
        ne = dict(e)
        ne.update({"grounded": grounded, "uniquely_grounded": uniquely, "evidence": hits})
        out.append(ne)
    return out


def main() -> None:
    args = sys.argv[1:]
    if len(args) < 2:
        print("usage: adobs-verify-citations <events.jsonl> <answer.txt>", file=sys.stderr)
        sys.exit(1)
    events_path, answer_path = args[0], args[1]

    events = [json.loads(l) for l in Path(events_path).read_text().split("\n") if l]
    answer = Path(answer_path).read_text()
    claimed = parse_cited(answer)
    if claimed is not None:
        for e in events:
            e["used_downstream"] = e.get("label") in claimed
    verified = verify(events, answer)

    self_cited = sum(1 for e in verified if e.get("used_downstream"))
    grounded = sum(1 for e in verified if e["grounded"])
    unique = sum(1 for e in verified if e["uniquely_grounded"])

    print("── CITATION VERIFICATION ─────────────────────────────────────")
    print(f"  queries                          {len(verified)}")
    print(f"  self-reported as cited           {self_cited}")
    print(f"  grounded (any value in answer)   {grounded}")
    print(f"  uniquely grounded (strong)       {unique}")
    print()
    for e in verified:
        if e.get("used_downstream") and not e["grounded"]:
            flag = " ← CLAIMED BUT UNGROUNDED"
        elif not e.get("used_downstream") and e["uniquely_grounded"]:
            flag = " ← used but not claimed"
        else:
            flag = ""
        label = (e.get("label") or "?").ljust(4)
        intent = (e.get("span_intent") or "")[:46]
        print(
            f"  {label} claim={'Y' if e.get('used_downstream') else 'n'} "
            f"grounded={'Y' if e['grounded'] else 'n'} unique={'Y' if e['uniquely_grounded'] else 'n'}  "
            f"{intent}{flag}"
        )

    Path(events_path).write_text("\n".join(json.dumps(e) for e in verified))


if __name__ == "__main__":
    main()
