"""Nothing fails silently. Every degraded component reaches the user.

THE GAP THIS CLOSES

Ten places in the new retrieval code caught an exception and returned an
empty result. Each looked defensible on its own — "vector search is one
signal, losing it should weaken the answer, not fail the request" — and
together they built exactly the blind spot a user cannot detect:

    embedder down      -> vector search returns []        -> lexical only
    Chroma missing     -> vector search returns []        -> lexical only
    web unreachable    -> world lookup returns []         -> no confirmation
    supervisor down    -> review returns allow            -> unchecked answer
    guard errors       -> check returns allowed           -> unchecked SQL

The answer still arrives. It looks complete. It was produced with half the
system, and nobody is told. That is worse than an error message, because an
error message is actionable and a quietly worse answer is not.

WHAT GOOD SOFTWARE DOES

When a search engine is down it says so. It does not return three results
and let you assume that is all there is. The user decides what to do with a
degraded answer; the software does not decide for them by staying quiet.

THE MODEL HERE

Every component reports into a ledger for the life of one request. At the
end the ledger says one of three things:

    HEALTHY    every component that was asked to run, ran
    DEGRADED   something failed; the answer stands but is incomplete, and
               the user is told which capability was missing and what that
               means for the answer
    UNUSABLE   something essential failed; no answer is given, and the user
               is told plainly that the system is down and what to do

Severity is a property of the component, not of the failure. Losing web
lookup weakens a geography question. Losing the language model means there
is no answer at all, and pretending otherwise is the blind spot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Severity(str, Enum):
    ESSENTIAL = "essential"     # no answer is possible without it
    IMPORTANT = "important"     # the answer stands but is materially weaker
    OPTIONAL = "optional"       # a refinement was skipped


@dataclass
class Degradation:
    component: str
    severity: Severity
    detail: str
    consequence: str            # plain words, for the person reading the answer
    affects: str = "any"        # 'text' | 'number' | 'any'

    def matters_for(self, answer_kind: str) -> bool:
        """Whether this degradation actually touched THIS answer.

        Measured mistake: a count of 73, computed entirely in SQL, was
        published with a warning that some passages could not be cited.
        True, irrelevant, and alarming — the count never used passages. A
        caveat attached to an answer it does not apply to teaches people to
        ignore caveats.
        """
        if self.severity is Severity.ESSENTIAL:
            return True
        if self.affects == "any":
            return True
        return self.affects == answer_kind


@dataclass
class Ledger:
    """Health of one request. Created per question, never shared."""

    events: list[Degradation] = field(default_factory=list)

    def record(self, component: str, severity: Severity, detail: str,
               consequence: str, affects: str = "any") -> None:
        self.events.append(Degradation(component, severity, detail[:300],
                                       consequence, affects))

    # Convenience recorders, so a call site cannot forget the consequence.

    def vector_unavailable(self, detail: str) -> None:
        self.record(
            "document search", Severity.IMPORTANT, detail,
            "I could only match documents on the exact words you used, so "
            "reports that describe the same thing differently may be "
            "missing", affects="text")

    def embedder_unavailable(self, detail: str) -> None:
        self.record(
            "document search", Severity.IMPORTANT, detail,
            "I could not match meaning across languages, so an English "
            "question may have missed Urdu records", affects="text")

    def web_unavailable(self, detail: str) -> None:
        self.record(
            "outside lookup", Severity.OPTIONAL, detail,
            "I could not check terms that are not defined in these case "
            "files", affects="any")

    def supervisor_unavailable(self, detail: str) -> None:
        self.record(
            "answer review", Severity.IMPORTANT, detail,
            "this answer was not double-checked before being shown to you",
            affects="any")

    def guard_skipped(self, which: str, detail: str) -> None:
        self.record(
            "accuracy check", Severity.IMPORTANT, detail,
            "one of the checks that catches wrong numbers did not run",
            affects="number")

    def model_unavailable(self, detail: str) -> None:
        self.record(
            "the system", Severity.ESSENTIAL, detail,
            "no answer could be produced")

    def data_incomplete(self, detail: str, consequence: str,
                        affects: str = "text") -> None:
        self.record("case records", Severity.IMPORTANT, detail, consequence,
                    affects)

    # ── Verdict ────────────────────────────────────────────────────────

    @property
    def status(self) -> str:
        if any(e.severity is Severity.ESSENTIAL for e in self.events):
            return "unusable"
        if any(e.severity in (Severity.IMPORTANT, Severity.OPTIONAL)
               for e in self.events):
            return "degraded"
        return "healthy"

    @property
    def usable(self) -> bool:
        return self.status != "unusable"

    def user_message(self, answer_kind: str = "any") -> str | None:
        """What the person is told. None when nothing relevant went wrong.

        Written for an investigator, not a developer. No component names
        from the codebase, no exception text, no JSON. The technical detail
        stays in as_dict() for logs and for whoever operates the system.

        answer_kind filters to what actually touched THIS answer: a count
        computed in SQL is not weakened by a document-search outage, and
        warning about it teaches people to ignore warnings.
        """
        essential = [e for e in self.events
                     if e.severity is Severity.ESSENTIAL]
        if essential:
            return ("I can't answer this right now — the system is not "
                    "fully available. This is a problem on our side, not "
                    "with your question. Please try again in a few minutes, "
                    "and let your administrator know if it keeps happening.")

        relevant = [e for e in self.events if e.matters_for(answer_kind)]
        if not relevant:
            return None

        # De-duplicate: two components failing the same way is one thing to
        # tell someone, not two.
        seen: list[str] = []
        for e in relevant:
            if e.consequence not in seen:
                seen.append(e.consequence)

        if len(seen) == 1:
            body = seen[0]
        else:
            body = "; ".join(seen)
        return (f"Note: part of the search was unavailable, so this answer "
                f"may be incomplete — {body}. It is worth re-running this "
                f"question later if the answer matters.")

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "events": [
                {"component": e.component, "severity": e.severity.value,
                 "detail": e.detail, "consequence": e.consequence}
                for e in self.events
            ],
        }
