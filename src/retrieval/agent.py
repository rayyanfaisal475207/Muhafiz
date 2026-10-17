"""The analysis agent: for questions one compiled query cannot answer.

WHERE THIS SITS

    bind + gate                  deterministic, ~100 ms, no model
      |
    escalation triggers?
      |-- no  -> compile one query, execute            the fast path
      |-- yes -> HERE

Escalation used to mean refusal, because this did not exist. Measured, that
refused questions the planner had understood perfectly: "which police
station handles the most cases" died because the verb "handles" matched no
column. The question was clear; the compiler simply had no template for it.

WHAT THE AGENT MAY AND MAY NOT DO

It may look at the schema, read what values exist, find join paths, check
coverage, and run read-only SQL — as many times as it needs, up to a bound.

It may NOT produce a number. Every figure in the final answer comes back
from a query result, because a model that reads rows and reports a total is
the counting-by-reading failure this rebuild exists to remove. That is how
the legacy engine answered 208 for a field that does not exist.

The loop is bounded, and it stops early when a turn adds nothing — an agent
that rephrases the same query three times is not converging, it is just
spending tokens.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import asyncpg

from src.llm.client import call_llm
from src.retrieval import tools as T
from src.retrieval.contract import Contract
from src.retrieval.supervisor import review_finish


# Declaring requirements and orienting cost turns the old budget did not
# account for. Measured: the agent found the right table, ran the right
# query and marked a requirement met on turn 10 — the last one — and so
# never answered. Work already done was thrown away for want of one turn.
MAX_TURNS = 16


@dataclass
class Step:
    turn: int
    tool: str
    argument: str
    ok: bool
    summary: str


@dataclass
class AgentAnswer:
    answered: bool
    text: str
    steps: list[Step] = field(default_factory=list)
    query_log: T.QueryLog = field(default_factory=T.QueryLog)
    refusal: str | None = None

    def evidence(self) -> list[dict]:
        return [e for e in self.query_log.entries if e["error"] is None]


SYSTEM = """You answer questions about a police case database by
INSPECTING it and QUERYING it. You never answer from memory.

FIRST TURN — declare what must be established:

  {"tool": "declare", "requirements": [
     {"statement": "the set of cases that involve a weapon"},
     {"statement": "how many distinct such cases there are"}]}

Requirements are what you must KNOW to answer, not steps you will take.
Two or three is usual. More than that means the question should be split.

EVERY LATER TURN — one JSON object, nothing else:

  {"tool": "schema_overview"}
  {"tool": "describe", "table": "weapons"}
  {"tool": "values", "table": "case_parties", "column": "role_raw"}
  {"tool": "find_term", "term": "heroin"}
  {"tool": "join_paths", "a": "weapons", "b": "places"}
  {"tool": "coverage", "table": "rebuild_cases", "column": "status_raw"}
  {"tool": "query", "sql": "SELECT ..."}
  {"tool": "met", "requirement": "R1", "evidence": "<what established it>"}
  {"tool": "unreachable", "requirement": "R2", "why": "<what is missing>"}
  {"tool": "answer", "text": "..."}
  {"tool": "refuse", "reason": "..."}

Mark a requirement `met` as soon as an observation establishes it, and
`unreachable` as soon as you are confident the data cannot supply it.
Those two calls are how you make progress — a query that satisfies nothing
is motion, not progress.

Rules:

1. NEVER state a number you did not get back from a tool. Let SQL do the
   counting with count(), sum(), avg().
2. Before filtering on a value, check it exists — `find_term` or `values`.
   The data is Urdu; an English guess matches nothing and returns a
   confident zero.
3. Use count(DISTINCT <primary key>) whenever your query joins.
4. Most fact tables are bitemporal: add `valid_to IS NULL` for what is
   true now.
5. Tables named in your briefing are a STARTING POINT. The schema has more.
   If something seems missing, call schema_overview and find_term before
   concluding it is absent.
6. If an approach has already failed, do something DIFFERENT — another
   table, another column, another join. Repeating it will not help.
7. Answer in the language the question was asked in."""


_JSON = re.compile(r"\{.*\}", re.S)


def _parse(raw: str) -> dict | None:
    m = _JSON.search(raw or "")
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _summarise(result: T.ToolResult, limit: int = 1200) -> str:
    """Compact a tool result for the next prompt turn.

    Kept small deliberately: the agent needs enough to decide, not the whole
    table. A large result is a signal to aggregate in SQL, not to page
    through rows in the context window.
    """
    payload = {"ok": result.ok}
    if result.error:
        payload["error"] = result.error
    if result.note:
        payload["note"] = result.note
    if result.data is not None:
        text = json.dumps(result.data, ensure_ascii=False, default=str)
        payload["data"] = (text if len(text) <= limit
                           else text[:limit] + f"... [truncated; "
                                               f"aggregate in SQL instead]")
    return json.dumps(payload, ensure_ascii=False)


async def _dispatch(conn: asyncpg.Connection, call: dict,
                    log: T.QueryLog) -> tuple[str, str, T.ToolResult]:
    """Route one tool call, validating its arguments first.

    The agent writes these calls, so they are untrusted input. A call with
    a missing argument reached a SQL function as NULL and raised

        null values cannot be formatted as an SQL identifier

    which killed the entire request. A malformed call must produce a
    correction the agent can act on, exactly like a malformed query does —
    never an exception that ends the conversation.
    """
    name = str(call.get("tool") or "").strip()

    def need(*keys: str) -> tuple[list[str], str | None]:
        vals, missing = [], []
        for k in keys:
            v = call.get(k)
            v = str(v).strip() if v is not None else ""
            if not v:
                missing.append(k)
            vals.append(v)
        if missing:
            return vals, (f"{name} needs {', '.join(missing)}. "
                          f"Send the full call, e.g. "
                          f'{{"tool": "{name}", '
                          + ", ".join(f'"{k}": "..."' for k in keys) + "}")
        return vals, None

    if name == "schema_overview":
        return name, "", await T.schema_overview(conn)

    if name in ("declare", "met", "unreachable"):
        # Contract calls are handled by the loop, which owns the state.
        return name, "", T.ToolResult(True, name, data=call)

    if name == "describe":
        (t,), err = need("table")
        if err:
            return name, "", T.ToolResult(False, name, error=err)
        return name, t, await T.describe(conn, t)

    if name == "values":
        (t, c), err = need("table", "column")
        if err:
            return name, "", T.ToolResult(False, name, error=err)
        return name, f"{t}.{c}", await T.values(conn, t, c)

    if name == "find_term":
        (term,), err = need("term")
        if err:
            return name, "", T.ToolResult(False, name, error=err)
        return name, term, await T.find_term(conn, term)

    if name == "join_paths":
        (a, b), err = need("a", "b")
        if err:
            return name, "", T.ToolResult(False, name, error=err)
        return name, f"{a}->{b}", await T.join_paths(conn, a, b)

    if name == "coverage":
        (t, c), err = need("table", "column")
        if err:
            return name, "", T.ToolResult(False, name, error=err)
        return name, f"{t}.{c}", await T.coverage(conn, t, c)

    if name == "query":
        (sql,), err = need("sql")
        if err:
            return name, "", T.ToolResult(False, name, error=err)
        from src.retrieval.guards import check_all
        guard = await check_all(conn, sql)
        if not guard.allowed:
            return name, sql[:120], T.ToolResult(
                False, "query", error=guard.correction)
        result = await T.query(conn, sql, log)
        if guard.warning and result.ok:
            result.note = ((result.note + " | ") if result.note else "")                 + guard.warning
        return name, sql[:120], result

    return name, "", T.ToolResult(
        False, name or "unknown",
        error=f"unknown tool {name!r}; valid tools are schema_overview, "
              f"describe, values, find_term, join_paths, coverage, query, "
              f"answer, refuse")


async def run_agent(conn: asyncpg.Connection, question: str,
                    hints: str = "", max_turns: int = MAX_TURNS,
                    provider: str | None = None,
                    supervise: bool = True,
                    max_redirects: int = 2,
                    ledger=None) -> AgentAnswer:
    log = T.QueryLog()
    contract = Contract()
    steps: list[Step] = []
    # Bounded: a supervisor that can redirect forever turns a wrong answer
    # into an infinite one.
    redirects = 0
    transcript: list[str] = []
    if hints:
        transcript.append(f"What the planner already established:\n{hints}")

    for turn in range(1, max_turns + 1):
        # The prompt leads with STATE, not history. A long transcript
        # buries what still matters; the contract says it in three lines,
        # and the exhausted list stops the agent re-treading dead ends.
        blocks = [f"Question: {question}", contract.status_line()]
        tried = contract.exhausted()
        if tried:
            blocks.append("Already tried and failed, do not repeat:"
                          + chr(10) + "  "
                          + (chr(10) + "  ").join(tried[:5]))
        blocks.append((chr(10) * 2).join(transcript[-6:]) if transcript
                      else "No observations yet.")
        forced = contract.directive()
        if forced:
            # A satisfied contract is a stop condition, not a suggestion.
            blocks.append("[required next] " + forced)
        elif not contract.declared:
            blocks.append("Declare your requirements first.")
        user = (chr(10) * 2).join(blocks) + (chr(10) * 2) + "Next JSON object:"
        try:
            raw = await call_llm(SYSTEM, user, temperature=0.0,
                                 max_tokens=2600,
                                 provider_override=provider,
                                 force_cloud=provider is not None)
        except Exception as exc:                        # noqa: BLE001
            # The language model is ESSENTIAL. Without it there is no
            # answer, and the user must be told that plainly rather than
            # handed a refusal that reads like a finding about their data.
            if ledger:
                ledger.model_unavailable(str(exc)[:200])
            return AgentAnswer(False, "", steps, log,
                               refusal=f"model unavailable: {exc}"[:200])

        call = _parse(raw)
        if call is None:
            transcript.append("Your reply was not a JSON object. Reply with "
                              "exactly one JSON object.")
            continue

        tool = str(call.get("tool") or "")

        if tool == "declare":
            items = call.get("requirements") or []
            if not isinstance(items, list) or not items:
                transcript.append("declare needs a non-empty requirements "
                                  "list. Each item needs a `statement`.")
                continue
            contract.declare(items)
            steps.append(Step(turn, "declare",
                              f"{len(contract.requirements)} requirements",
                              True, contract.status_line()[:200]))
            transcript.append(f"[turn {turn}] declared" + chr(10)
                              + contract.status_line())
            continue

        if tool in ("met", "unreachable"):
            key = str(call.get("requirement") or "").strip()
            if tool == "met":
                detail = str(call.get("evidence") or "").strip()
                ok = contract.satisfy(key, detail or "(no evidence given)")
            else:
                detail = str(call.get("why") or "").strip()
                ok = contract.block(key, detail or "(no reason given)")
            steps.append(Step(turn, tool, key, ok, detail[:150]))
            transcript.append(
                f"[turn {turn}] {tool} {key}: "
                + ("recorded" if ok else
                   f"{key} is not an open requirement; open ones are "
                   + ", ".join(r.key for r in contract.open_requirements())))
            continue

        # A question must be understood before it can be answered. Without
        # requirements there is no definition of done, which is exactly how
        # ten turns of reasonable-looking queries produced no answer.
        if not contract.declared and tool not in ("refuse",):
            transcript.append("Declare your requirements before using any "
                              "other tool.")
            continue

        if tool == "answer":
            text = str(call.get("text") or "").strip()
            # An answer with figures must rest on a query that returned
            # rows. Without that the number came from the model, which is
            # precisely what this design forbids.
            if _has_number(text) and not log.numbers_are_sourced():
                transcript.append(
                    "That answer states a number but no query has returned "
                    "any rows. Run a query that computes it, then answer.")
                continue

            # A second model reviews the trace before this becomes final.
            # Hard checks catch what can be stated exactly; this catches
            # judgement — answering about people from a table with no
            # people in it, or stopping a tool short.
            if supervise and redirects < max_redirects:
                rev = await review_finish(question, "answer", text,
                                          steps, log.evidence_entries())
                if not rev.allow:
                    redirects += 1
                    transcript.append(
                        f"[supervisor] Not yet. {rev.reason or ''} "
                        f"Do this next: {rev.instruction}")
                    continue
            return AgentAnswer(True, text, steps, log)

        if tool == "refuse":
            reason = str(call.get("reason") or "").strip()
            # A refusal must be EARNED by looking. Measured: asked how many
            # individuals were arrested in drug cases, the agent refused on
            # turn 1 with zero tool calls — reasoning from the two tables in
            # its hints, while case_parties and case_events sat unexamined.
            #
            # Rule 6 already told it to look first. A prompt rule is a
            # request; the loop is where it becomes a requirement. This is
            # the same lesson as escalation: detecting a condition and then
            # not acting on it is no better than not detecting it.
            inspected = {s.tool for s in steps}
            if not (inspected & {"schema_overview", "describe", "find_term",
                                 "values", "join_paths"}):
                transcript.append(
                    "You have not inspected anything yet, so you cannot know "
                    "what is missing. The tables you were given are a "
                    "starting point, not the schema. Call schema_overview, "
                    "then find_term for the words you think are absent. "
                    "Refuse only after looking.")
                continue
            if not log.entries:
                transcript.append(
                    "You have inspected but never queried. If the columns "
                    "exist, run the query before concluding the data cannot "
                    "answer this.")
                continue

            if supervise and redirects < max_redirects:
                rev = await review_finish(question, "refuse", reason,
                                          steps, log.evidence_entries())
                if not rev.allow:
                    redirects += 1
                    transcript.append(
                        f"[supervisor] Not yet. {rev.reason or ''} "
                        f"Do this next: {rev.instruction}")
                    continue
            return AgentAnswer(False, "", steps, log, refusal=reason)

        try:
            name, arg, result = await _dispatch(conn, call, log)
        except Exception as exc:                        # noqa: BLE001
            # A tool raising is a fault in the tool, not a reason to abandon
            # the question. The agent is told and can try something else.
            name = str(call.get("tool") or "unknown")
            arg = ""
            result = T.ToolResult(False, name,
                                  error=f"that call failed: {exc}"[:220])
        contract.record(name, arg, result.ok, result.error or result.note or "")
        if result.ok and result.data is not None:
            log.observe(result.data)
        # Guard warnings and corrections carry real figures — "recorded for
        # only 17 of 228 rows" comes from the database. The agent quoted
        # those and the validator called them unsourced, blocking an answer
        # built on numbers the system itself had supplied.
        for text in (result.note, result.error):
            if text:
                log.observe(text)
        summary = _summarise(result)
        steps.append(Step(turn, name, arg, result.ok, summary[:200]))
        transcript.append(f"[turn {turn}] {name}({arg})\n{summary}")

        # Repetition counted by the contract, successes included. Ten
        # successful runs of one query is not progress — measured, that is
        # exactly how a question burned every turn without answering.
        seen = contract.repeated(name, arg)
        if seen >= 3:
            open_reqs = ", ".join(
                f"{r.key} ({r.statement[:60]})"
                for r in contract.open_requirements()) or "none"
            transcript.append(
                f"You have run {name} on the same input {seen} times. It "
                f"will not give a different result. Still open: {open_reqs}. "
                f"Either mark a requirement met from what you already have, "
                f"try a DIFFERENT table or column, or mark it unreachable.")
            continue

        # Stop when a turn adds nothing new: repeating a call — succeeding
        # or not — is not convergence.
        if len(steps) >= 4 and _stalled(steps):
            return AgentAnswer(
                False, "", steps, log,
                refusal="the agent repeated the same unsuccessful step; "
                        "stopping rather than spending more turns")

    # A turn-limit refusal must still report what was established, or the
    # work is invisible and the user cannot tell a hard question from a
    # broken one.
    met = [f"{r.key} ({r.evidence})" for r in contract.requirements
           if r.satisfied]
    still_open = [f"{r.key}: {r.statement}"
                  for r in contract.open_requirements()]
    detail = []
    if met:
        detail.append("established " + "; ".join(met))
    if still_open:
        detail.append("could not establish " + "; ".join(still_open))
    return AgentAnswer(
        False, "", steps, log,
        refusal=(f"ran out of steps after {max_turns} turns and "
                 f"{len(log.entries)} queries"
                 + (" — " + ", ".join(detail) if detail else "")))


_NUM = re.compile(r"\b\d[\d,]*\b")


def _has_number(text: str) -> bool:
    return bool(_NUM.search(text or ""))


def _stalled(steps: list[Step]) -> bool:
    """Repetition, whether or not the repeated step succeeds.

    The first version required three consecutive FAILURES. Measured, an
    agent ran ten near-identical queries that alternated success and
    failure and never answered — so the detector never fired and the loop
    burned every turn. Success is not progress if it is the same success
    over and over.
    """
    last = steps[-3:]
    if len(last) < 3:
        return False
    return len({(s.tool, s.argument[:60]) for s in last}) == 1


def _repeating(steps: list[Step]) -> str | None:
    """A nudge before the hard stop, naming what is being repeated.

    Stopping outright wastes the work already done. Telling the agent it
    is circling — and that it already has what it needs — recovers the
    turn instead of spending it.
    """
    if len(steps) < 3:
        return None
    recent = [(s.tool, s.argument[:60]) for s in steps[-3:]]
    if len(set(recent)) > 1:
        return None
    tool, arg = recent[0]
    return (f"You have called {tool} on the same input three times. Either "
            f"answer from what you already have, try a DIFFERENT table or "
            f"column, or refuse and say what is missing. Repeating this "
            f"will not change the result.")
