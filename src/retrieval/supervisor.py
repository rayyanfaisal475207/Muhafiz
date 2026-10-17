"""A second model call that watches the first one and pushes it back on track.

WHY THIS AND NOT MORE RULES

The agent runs on a quantized local model. Loading its context with schema,
values and prior observations makes it skip steps — not because it disagrees
with the instructions, but because instructions compete with everything else
in the window. Measured: told plainly to inspect before refusing, it refused
on turn one having called no tool at all.

Hard checks catch the cases that can be stated exactly — a filter on a value
that does not exist, an aggregate over a join without DISTINCT. They cannot
catch judgement: answering a question about people from a table with no
people in it, or stopping three tools short of the answer.

So this is a supervisor, not another rule. It reads what the agent is about
to do, in the context of what it has already done, and either lets it pass
or hands back a specific instruction. Nothing here decides what the answer
is; it decides only whether the agent has done enough to be allowed to say
one.

WHY IT IS CHEAP ENOUGH

It only runs on the two actions that END the loop — `answer` and `refuse`.
Every tool call passes untouched. A wrong answer costs far more than an
extra call, and the tool calls themselves are already bounded.

WHAT IT IS NOT

It is not a second opinion on the number. It never sees a chance to invent
one: it is given the agent's proposed action and its trace, and replies with
`allow` or `redirect`. If it fails or is unavailable, the action is ALLOWED
— a supervisor that blocks answers when the model is down would turn a
degraded service into a dead one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from src.llm.client import call_llm


@dataclass
class Review:
    allow: bool
    instruction: str | None = None
    reason: str | None = None


SYSTEM = """You supervise an agent answering questions from a police case
database. The agent has just proposed to FINISH — either by answering or by
refusing. Decide whether it has earned that.

Reply with ONLY this JSON:

  {"verdict": "allow"}
  {"verdict": "redirect", "instruction": "<one concrete next step>",
   "reason": "<short>"}

Redirect when:
  * it refuses but has not inspected the obvious places. Tables it was
    handed are a starting point, not the schema. If the question is about
    people and it never looked at a people table, redirect.
  * it answers with a figure that no query in its trace produced.
  * it filtered on a value without checking that value exists.
  * the question asks about several things and it addressed one.
  * it stopped while an obvious, cheap next query remained.

Allow when:
  * the answer follows from the queries actually run, OR
  * the refusal follows from having looked and genuinely found nothing.

A refusal that names precisely what is missing, after looking, is a GOOD
outcome. Do not redirect an honest refusal into a guess.

`instruction` must name a tool and its argument, e.g.
"call describe on case_parties" or
"run SELECT count(DISTINCT person_id) FROM case_events WHERE ...".
Vague advice is useless to the agent."""


_JSON = re.compile(r"\{.*\}", re.S)


def _trace(steps, query_entries) -> str:
    lines = []
    for s in steps:
        lines.append(f"  turn {s.turn}: {s.tool}({s.argument[:70]}) "
                     f"{'ok' if s.ok else 'FAILED'}")
    if query_entries:
        lines.append("  queries that returned rows:")
        for e in query_entries:
            if e.get("error") is None:
                lines.append(f"    {e['rows']} rows <- {e['sql'][:150]}")
    else:
        lines.append("  no successful queries")
    return "\n".join(lines) or "  (nothing done yet)"


async def review_finish(question: str, action: str, payload: str,
                        steps, query_entries,
                        provider: str | None = None, ledger=None) -> Review:
    """Check a proposed answer or refusal before it becomes final."""
    user = (
        f"Question: {question}\n\n"
        f"What the agent did:\n{_trace(steps, query_entries)}\n\n"
        f"It now proposes to {action}:\n{payload[:900]}\n\n"
        f"Verdict JSON:")

    try:
        raw = await call_llm(SYSTEM, user, temperature=0.0, max_tokens=2000,
                             provider_override=provider,
                             force_cloud=provider is not None)
    except Exception as exc:                            # noqa: BLE001
        # Allow, but SAY SO. Blocking every answer when a second model is
        # down turns degradation into an outage; staying quiet about it
        # turns a reviewed answer into an unreviewed one that looks the
        # same. The user is told which it was.
        if ledger:
            ledger.supervisor_unavailable(str(exc)[:200])
        return Review(True, reason="supervisor unavailable")

    m = _JSON.search(raw or "")
    if not m:
        return Review(True, reason="supervisor gave no parseable verdict")
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return Review(True, reason="supervisor gave malformed JSON")

    if str(obj.get("verdict", "")).lower() != "redirect":
        return Review(True)

    instruction = str(obj.get("instruction") or "").strip()
    if not instruction:
        # A redirect with no concrete step is not actionable, and looping on
        # it would burn turns without progress.
        return Review(True, reason="redirect had no instruction")
    return Review(False, instruction=instruction,
                  reason=str(obj.get("reason") or "")[:200])
