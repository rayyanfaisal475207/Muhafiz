"""What the agent must establish, tracked in code rather than hoped for.

THE PROBLEM THIS SOLVES

The agent had a question and tools and nothing in between. Measured, it ran
ten near-identical queries on the wrong table and never answered — every
turn locally reasonable, no turn closer to done. It could not tell progress
from motion, because nothing defined "done".

    no requirements  -> "done" is undefined -> the loop only ends on a
                        turn limit, which is not an answer

THE SHAPE

    turn 1   the model DECLARES what must be established
    turns    each observation is bound to a requirement
    code     tracks satisfaction, repetition and reachability
    end      all satisfied -> answer; any unreachable -> refuse, naming it

The model decides WHAT the requirements are — that part cannot be
enumerated, because questions cannot be enumerated. Code decides whether
they are MET, which is decidable without knowing what the question was
about. That split is what makes this general: it carries no knowledge of
police data, counts, weapons or any other subject.

WHY THIS BEATS MORE PROMPT RULES

A rule says "answer when you have enough". That is not a criterion and a
quantized model cannot act on it. A requirement list with satisfaction
flags is a criterion: the loop can see that R1 has evidence and R2 does
not, and say so in those words.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Requirement:
    """One thing that must be established before an answer is possible."""

    key: str                    # R1, R2 …
    statement: str              # what must be established, in the model's words
    satisfied: bool = False
    evidence: str | None = None      # how it was satisfied
    unreachable: str | None = None   # why it cannot be

    @property
    def open(self) -> bool:
        return not self.satisfied and self.unreachable is None


@dataclass
class Attempt:
    """A (tool, target) pair that has been tried, and how it went."""

    tool: str
    target: str
    ok: bool
    note: str = ""

    @property
    def signature(self) -> tuple[str, str]:
        return (self.tool, self.target[:60])


@dataclass
class Contract:
    requirements: list[Requirement] = field(default_factory=list)
    attempts: list[Attempt] = field(default_factory=list)

    # ── State ──────────────────────────────────────────────────────────

    @property
    def declared(self) -> bool:
        return bool(self.requirements)

    @property
    def complete(self) -> bool:
        return self.declared and all(r.satisfied for r in self.requirements)

    @property
    def blocked(self) -> bool:
        """Nothing left that could still be established."""
        return self.declared and not any(r.open for r in self.requirements) \
            and not self.complete

    def open_requirements(self) -> list[Requirement]:
        return [r for r in self.requirements if r.open]

    # ── Updates ────────────────────────────────────────────────────────

    def declare(self, items: list[dict]) -> None:
        self.requirements = [
            Requirement(key=f"R{i + 1}",
                        statement=str(it.get("statement") or it)[:300])
            for i, it in enumerate(items[:6])       # more than six is a sign
        ]                                           # the question needs splitting

    def satisfy(self, key: str, evidence: str) -> bool:
        for r in self.requirements:
            if r.key.lower() == key.lower() and r.open:
                r.satisfied = True
                r.evidence = evidence[:200]
                return True
        return False

    def block(self, key: str, why: str) -> bool:
        for r in self.requirements:
            if r.key.lower() == key.lower() and r.open:
                r.unreachable = why[:200]
                return True
        return False

    def record(self, tool: str, target: str, ok: bool, note: str = "") -> None:
        self.attempts.append(Attempt(tool, target, ok, note))

    # ── What the loop needs to decide ──────────────────────────────────

    def repeated(self, tool: str, target: str, threshold: int = 2) -> int:
        """How many times this exact approach has already been tried.

        Counts successes too. Ten successful runs of the same query is not
        progress, and the earlier detector — which only counted failures —
        never fired on exactly that pattern.
        """
        sig = (tool, target[:60])
        return sum(1 for a in self.attempts if a.signature == sig)

    def exhausted(self) -> list[str]:
        """Approaches already tried and failed, worth saying out loud.

        A long transcript buries failures. Naming them forces a different
        approach instead of a variation on the one that did not work.
        """
        failed: dict[tuple[str, str], str] = {}
        for a in self.attempts:
            if not a.ok:
                failed[a.signature] = a.note
        return [f"{t}({g}) — {why[:90]}" for (t, g), why in failed.items()]

    def status_line(self) -> str:
        """The state of the contract, for the next prompt turn."""
        if not self.declared:
            return "No requirements declared yet."
        lines = []
        for r in self.requirements:
            if r.satisfied:
                mark = f"MET — {r.evidence}"
            elif r.unreachable:
                mark = f"UNREACHABLE — {r.unreachable}"
            else:
                mark = "STILL OPEN"
            lines.append(f"  {r.key}: {r.statement}  [{mark}]")
        return "Requirements:\n" + "\n".join(lines)

    def directive(self) -> str | None:
        """What the loop should force next, if anything.

        Returned as an instruction to the model rather than a silent
        decision, so its next turn is aimed rather than free.
        """
        if not self.declared:
            return None
        if self.complete:
            return ("Every requirement is met. Answer now, using only the "
                    "figures your observations produced. Do not run more "
                    "queries.")
        if self.blocked:
            unreachable = [f"{r.key} ({r.unreachable})"
                           for r in self.requirements if r.unreachable]
            return (f"No requirement can still be established: "
                    f"{'; '.join(unreachable)}. Refuse now and say exactly "
                    f"which of these the data cannot supply.")
        return None

    def as_dict(self) -> dict:
        return {
            "requirements": [
                {"key": r.key, "statement": r.statement,
                 "satisfied": r.satisfied, "evidence": r.evidence,
                 "unreachable": r.unreachable}
                for r in self.requirements
            ],
            "attempts": len(self.attempts),
            "complete": self.complete,
            "blocked": self.blocked,
        }


_KEY = re.compile(r"\bR(\d+)\b", re.I)


def keys_mentioned(text: str) -> list[str]:
    """Requirement keys a reply refers to, so satisfaction can be bound."""
    return [f"R{m.group(1)}" for m in _KEY.finditer(text or "")]
