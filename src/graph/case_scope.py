# ============================================================
# Case-scoping chokepoint for Apache AGE Cypher queries (Phase 2, Module
# 2.1.3 — see solution.md and issues.md's High "Apache AGE graph data has
# zero database-level access control" finding).
#
# AGE has no native row-level-security equivalent (its vertex/edge labels
# are catalog-backed tables, but matching AGE's internal row shape to an
# RLS predicate isn't a supported, documented AGE interface — see
# age_client.py's own docstring on why cypher_query/graph must always be
# trusted string constants, never request-built). The closest structural
# equivalent this build can offer is a single enforcement point: every
# Cypher template that is SUPPOSED to be scoped to one case must be run
# through `scoped_cypher()` below, which refuses to execute a template
# that doesn't actually reference `$case_id` in its text — turning "a
# template silently missing its case filter" from a silent cross-case leak
# into an immediate, loud failure.
#
# THIS IS DELIBERATELY NOT APPLIED TO EVERY CYPHER TEMPLATE IN THE
# CODEBASE. Reading entity_resolution.py, graph_retriever.py, versioning.py,
# and graph_review.py in full (during Phase 2 implementation) found that a
# large fraction of existing templates are CORRECTLY, DELIBERATELY
# cross-case by design, not oversights:
#   - entity_resolution.py's _fetch_all_nodes/_find_by_primary_id: CNIC/
#     plate uniqueness and cross-case repeat-offender resolution are
#     explicitly documented as needing to compare against every existing
#     node regardless of case.
#   - graph_retriever.py's _expand_confirmed_identity/_one_hop_neighbors/
#     _fetch_appears_in/_unconfirmed_same_as_links: these operate on an
#     already-resolved entity_id frontier (scoped upstream by
#     _filter_to_case at each hop when not cross-case), not on a case_id
#     directly — there is nothing for them to filter by case that isn't
#     already handled by the caller.
#   - graph_review.py's entire review queue: deliberately cross-case by
#     product design pending solution.md §9.2's open decision — forcing
#     case-scoping here would be a silent, unreviewed product change, not
#     a hygiene fix.
#   - versioning.py's write_node/write_edge: these don't take a fixed
#     Cypher template from the caller at all — they build Cypher
#     dynamically from a caller-supplied `match` dict that only
#     SOMETIMES includes case_id (e.g. a BELONGS_TO_CASE edge's `to_match`
#     does; a Person/Vehicle node's own identity match does not). There is
#     no static template to validate a `$case_id` reference against.
#
# Forcing all of the above through a "must reference $case_id or fail"
# gate would either break intentional cross-case behavior or have to be
# silently disabled for most call sites — worse than not having the gate
# at all. So `scoped_cypher()` is applied only to the templates that are
# actually meant to be single-case-scoped: graph_retriever.py's
# within-case seed lookup, case-wide entity enumeration, per-hop case
# filter, and conflict lookup, plus entity_resolution.py's case-membership
# check. See each call site's own comment for why it's routed through
# this chokepoint.
#
# [Milestone E2 — GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md] This module was
# already the production enforcement chokepoint before E2 (see the harness
# call sites, timeline_building.py/data_quality.py, and every graph_retriever.py/
# entity_resolution.py site named above) — E2's premise that it was
# "eval-only" was wrong and was corrected before any code changed. E2's
# REAL gap, found by tracing every path that grows graph_retriever.py's
# `visited` set: the per-hop case filter (`_filter_to_case`, this module's
# `scoped_cypher()` underneath) was only ever applied to ordinary
# ASSOCIATED_WITH hop results, never to `_expand_confirmed_identity()`'s
# identity fold — a CONFIRMED SAME_AS edge is often itself a cross-case
# link (the same person recognized in two FIRs), so a within-case query
# could silently pull another case's node straight into `visited` without
# ever passing through `_enforce_cross_case_role_gate()`. Fixed by routing
# the identity fold's `new_identity` set through the same `_filter_to_case`
# call the ordinary hop path already used, when `cross_case=False` — see
# `retrieve_graph()`'s hop loop in graph_retriever.py.
# ============================================================

from __future__ import annotations

from typing import Optional, Sequence

from src.graph import age_client


def _references_case_id(cypher_query: str) -> bool:
    return "$case_id" in cypher_query


async def scoped_cypher(
    cypher_query: str,
    case_id: str,
    params: Optional[dict] = None,
    columns: Sequence[str] = ("result",),
    graph: str = age_client.GRAPH_NAME,
) -> list[dict]:
    """
    Execute a Cypher template that MUST be scoped to one case.

    Refuses (raises `ValueError`, not a log line) rather than executing
    the query, if:
      - `case_id` is empty/None — a case-scoped chokepoint call with no
        case to scope to is a caller bug, not a query to run unscoped; or
      - `cypher_query` doesn't reference `$case_id` at all — a future
        edit that drops the case filter from a template registered here
        fails loudly and immediately instead of silently returning
        cross-case rows.

    `case_id` is always merged into `params` under the `case_id` key —
    callers must not also pass a `case_id` key in `params` (would collide).

    `graph` defaults to the production graph. entity_resolution.py's
    `_shares_case_batch` is the one call site that overrides it, so that
    when scripts/eval_entity_resolution.py resolves against
    `evidence_graph_eval` (Phase 3, Module 3.1), its case-membership check
    reads from the same graph its writes land in, rather than silently
    reading real case data from production.
    """
    if not case_id:
        raise ValueError(
            "case_scope.scoped_cypher() requires a non-empty case_id. "
            "For a deliberately cross-case query, call "
            "age_client.execute_cypher() directly instead — see this "
            "module's docstring for which templates that applies to."
        )
    if not _references_case_id(cypher_query):
        raise ValueError(
            "case_scope.scoped_cypher() was given a Cypher template with "
            "no $case_id reference — refusing to run it. Every template "
            "routed through this chokepoint must filter on $case_id in "
            "its WHERE/pattern:\n" + cypher_query
        )
    if params and "case_id" in params:
        raise ValueError("case_scope.scoped_cypher()'s params must not already contain 'case_id'.")

    merged_params = {**(params or {}), "case_id": case_id}
    return await age_client.execute_cypher(cypher_query, params=merged_params, columns=columns, graph=graph)
