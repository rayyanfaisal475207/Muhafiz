"""Derive binding thresholds from measured separation, not from intuition.

THE PROBLEM WITH THE CURRENT NUMBERS

The retrieval pipeline carries roughly fifteen hand-picked constants — a
0.45 binding floor, a 0.60 keep floor, a 10% ambiguity window, a 0.25
cross-script floor. Every one was chosen by judgement and then adjusted
when something failed, which is the authored-vocabulary mistake expressed
in numbers: a value that looks principled, is not measured, and is wrong
in a way nobody can see.

Two of them were already caught doing damage. 0.45 excluded `rebuild_cases`
at 0.429, so every question about "cases" refused. The 10% window flagged
a stored value as rivalled by a table whose name merely sounded similar.

HOW A THRESHOLD CAN BE MEASURED WITHOUT LABELLED DATA

The schema labels itself. For any stored value, we already know which
column it came from, so:

    TRUE MATCH   a value queried against the schema should bind to the
                 column it actually lives in
    FALSE MATCH  the same query binding to some OTHER column is, by
                 construction, wrong

Running real values through the real binder produces both distributions.
The threshold is then the point that separates them, chosen from measured
quantiles rather than a round number.

WHAT IS REPORTED

Separation is reported honestly, including when it is poor. If true and
false matches overlap heavily, no threshold fixes it and the answer is a
better signal, not a better cut-off. A calibration that always emits a
confident number would be the same failure one level up.

Results are stored with the sample size and the date they were measured,
because a threshold derived from 73 cases is not evidence about 73,000.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(".env")

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def dsn() -> str:
    url = os.getenv("DATABASE_URL", "")
    if not url:
        sys.exit("DATABASE_URL is not set")
    return url.replace("postgresql+asyncpg://", "postgresql://")


@dataclass
class Sample:
    term: str
    home_table: str
    home_column: str


async def draw_samples(conn: asyncpg.Connection, per_column: int,
                       max_columns: int) -> list[Sample]:
    """Take real stored values from real evidence columns.

    Only columns the binder would actually search, so the calibration
    measures the binder as it runs rather than an idealised version.
    """
    cols = await conn.fetch(
        """
        SELECT cc.table_name, cc.column_name
        FROM column_catalog cc
        JOIN table_roles tr ON tr.table_name = cc.table_name
        LEFT JOIN column_stats cs ON cs.table_name = cc.table_name
                                 AND cs.column_name = cc.column_name
        WHERE tr.is_evidence AND tr.role IN ('fact','dimension','entity')
          AND cc.is_text AND NOT cc.is_generated AND NOT cc.is_primary_key
          AND cc.references_table IS NULL
          AND coalesce(cs.rows_estimate, 0) > 0
        ORDER BY cc.table_name, cc.column_name
        """)

    out: list[Sample] = []
    for c in cols[:max_columns]:
        try:
            rows = await conn.fetch(
                f'SELECT DISTINCT "{c["column_name"]}"::text AS v '
                f'FROM "{c["table_name"]}" '
                f'WHERE "{c["column_name"]}" IS NOT NULL '
                f"  AND btrim(\"{c['column_name']}\"::text) <> '' "
                f"LIMIT 200")
        except Exception:                               # noqa: BLE001
            continue
        vals = [r["v"] for r in rows if r["v"] and len(r["v"]) >= 3]
        random.shuffle(vals)
        for v in vals[:per_column]:
            out.append(Sample(v[:80], c["table_name"], c["column_name"]))
    return out


async def measure(conn: asyncpg.Connection,
                  samples: list[Sample]) -> dict[str, dict[str, list[float]]]:
    """Score every sample and split results into true and false matches.

    Truth is VERIFIED, not assumed. The first version labelled a match true
    only if it landed on the column the value was drawn from — and reported
    false matches scoring a perfect 1.000, which was the measurement being
    wrong rather than the binder.

    A value does not live in exactly one column. The same drug name appears
    in seized_items.description_ur and in case_events.description_ur, and
    both are correct answers to "where does this term occur". So each match
    is checked against the database: does the term actually occur in that
    column? A match that does is true wherever it landed; one that does not
    is false.
    """
    by_kind: dict[str, dict[str, list[float]]] = {}
    verified: dict[tuple[str, str, str], bool] = {}

    async def occurs(term: str, table: str, column: str) -> bool:
        key = (term, table, column)
        if key in verified:
            return verified[key]
        try:
            hit = await conn.fetchval(
                f'SELECT EXISTS (SELECT 1 FROM "{table}" '
                f' WHERE "{column}"::text ILIKE $1)', f"%{term}%")
        except Exception:                               # noqa: BLE001
            hit = False
        verified[key] = bool(hit)
        return verified[key]

    for s in samples:
        try:
            rows = await conn.fetch(
                "SELECT match_kind, table_name, column_name, score"
                " FROM search_schema($1, 5000, 0.10, 40)", s.term)
        except Exception:                               # noqa: BLE001
            continue
        for r in rows:
            if r["match_kind"] not in ("value", "contains", "cross_script"):
                continue
            score = float(r["score"] or 0)
            kind = r["match_kind"]
            bucket = by_kind.setdefault(kind, {"true": [], "false": []})
            if await occurs(s.term, r["table_name"], r["column_name"]):
                bucket["true"].append(score)
            else:
                bucket["false"].append(score)
    return by_kind


def quantile(xs: list[float], q: float) -> float:
    if not xs:
        return float("nan")
    ys = sorted(xs)
    idx = min(len(ys) - 1, max(0, int(round(q * (len(ys) - 1)))))
    return ys[idx]


def separation(true_s: list[float], false_s: list[float]) -> dict:
    """Where to cut, and how much to trust the cut.

    The floor is set at the 95th percentile of FALSE matches: above it,
    one in twenty false matches survives. Recall at that floor says what
    it costs. When the two distributions overlap badly the report says so
    rather than emitting a confident number.
    """
    if not true_s or not false_s:
        return {"usable": False,
                "reason": f"insufficient sample "
                          f"(true={len(true_s)}, false={len(false_s)})"}

    floor = quantile(false_s, 0.95)
    recall = sum(1 for t in true_s if t >= floor) / len(true_s)
    t_med = statistics.median(true_s)
    f_med = statistics.median(false_s)

    return {
        "usable": recall >= 0.80,
        "suggested_floor": round(floor, 3),
        "recall_at_floor": round(recall, 3),
        "true_median": round(t_med, 3),
        "false_median": round(f_med, 3),
        "true_p05": round(quantile(true_s, 0.05), 3),
        "false_p95": round(floor, 3),
        "gap": round(t_med - f_med, 3),
        "n_true": len(true_s),
        "n_false": len(false_s),
        "reason": ("clean separation" if recall >= 0.80 else
                   "true and false matches overlap: no threshold separates "
                   "them, so the binder needs a better signal rather than a "
                   "better cut-off"),
    }


STORE = """
CREATE TABLE IF NOT EXISTS derived_thresholds (
    name          text PRIMARY KEY,
    value         numeric NOT NULL,
    measured_at   timestamptz NOT NULL DEFAULT now(),
    sample_size   integer,
    recall        numeric,
    method        text,
    notes         text
);

COMMENT ON TABLE derived_thresholds IS
'Thresholds measured from the data rather than chosen by hand, with the '
'sample they came from. A threshold derived on 73 cases is not evidence '
'about 73,000 — re-run the calibration when the corpus changes size.';
"""


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--per-column", type=int, default=6)
    ap.add_argument("--max-columns", type=int, default=25)
    ap.add_argument("--write", action="store_true",
                    help="store the derived thresholds")
    args = ap.parse_args()

    random.seed(7)                        # reproducible sampling
    conn = await asyncpg.connect(dsn())
    try:
        samples = await draw_samples(conn, args.per_column, args.max_columns)
        print(f"Sampled {len(samples)} real values from the evidence surface")
        if not samples:
            sys.exit("no values available — ingestion has not loaded enough yet")

        by_kind = await measure(conn, samples)

        # Each match kind is a different KIND of evidence, measured on a
        # different scale. A single global cut-off across them was proven
        # unusable: it discarded 40% of correct matches to remove 95% of
        # wrong ones, because an exact equality of 1.0 and a name
        # resemblance of 0.5 are not points on one axis.
        if args.write:
            await conn.execute(STORE)

        stored = 0
        for kind in sorted(by_kind):
            res = separation(by_kind[kind]["true"], by_kind[kind]["false"])
            print(chr(10) + f"[{kind}]")
            for k, v in res.items():
                print(f"  {k:>18}: {v}")

            if not res.get("usable"):
                print(f"  -> not stored; {res['reason']}")
                continue
            if not args.write:
                stored += 1
                continue

            await conn.execute(
                """
                INSERT INTO derived_thresholds
                    (name, value, sample_size, recall, method, notes)
                VALUES ($1, $2, $3, $4,
                        'p95 of verified false-match scores for this kind',
                        $5)
                ON CONFLICT (name) DO UPDATE SET
                    value = EXCLUDED.value, measured_at = now(),
                    sample_size = EXCLUDED.sample_size,
                    recall = EXCLUDED.recall, notes = EXCLUDED.notes
                """,
                f"min_score.{kind}", res["suggested_floor"],
                res["n_true"] + res["n_false"], res["recall_at_floor"],
                f"true median {res['true_median']}, false median "
                f"{res['false_median']}, gap {res['gap']}")
            stored += 1
            print(f"  -> stored as min_score.{kind} = {res['suggested_floor']}")

        print(chr(10) + f"{stored} of {len(by_kind)} match kinds produced "
          f"a usable threshold.")
        if args.write and stored:
            for row in await conn.fetch(
                "SELECT name, value, recall, sample_size FROM derived_thresholds"
                " ORDER BY name"):
                print(f"  {row['name']:>26} = {row['value']} "
                      f"(recall {row['recall']}, n={row['sample_size']})")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
