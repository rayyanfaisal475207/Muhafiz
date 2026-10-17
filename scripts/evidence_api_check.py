"""Drive the real chat API end to end, across the kinds of question asked.

Not the runtime directly -- the HTTP endpoint the frontend calls, through
authentication, session creation and the SSE stream. That is the only
path that proves the product rather than a component.

Case kinds deliberately mixed, because each fails differently:

    count        an exact figure over a stated population
    descriptive  what the records say, from spans
    analysis     a pattern across records
    identity     a person across more than one record
    absent       something the data cannot support -- refusing IS the pass
    followup     a second turn that must inherit the first
    bilingual    the same question in Urdu

Every case records status, latency, whether a figure was asserted, and
whether coverage was stated. A confident figure without complete coverage
is reported as a failure regardless of whether the number looks right.

    python scripts/evidence_api_check.py
    python scripts/evidence_api_check.py --only count
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(".env", override=True)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

UR_MONTH = ("جون 2026 میں کتنی "
            "ایف آئی آر "
            "رپورٹ ہوئیں؟")
UR_WEAPON = ("مقدمات میں کون "
             "سے ہتھیار درج "
             "ہوئے؟")


@dataclass
class Case:
    name: str
    kind: str
    question: str
    # Refusing is correct: the records cannot support this.
    must_refuse: bool = False
    follows: str | None = None


CASES: list[Case] = [
    Case("count_reported", "count",
         "How many FIRs were reported in June 2026?"),
    Case("count_weapons", "count",
         "How many weapon register entries are recorded?"),
    Case("descriptive_fraud", "descriptive",
         "What methods do suspects use to commit fraud?"),
    Case("analysis_stations", "analysis",
         "Which police stations appear most often in these records?"),
    Case("identity_cross", "identity",
         "Which people appear in more than one record?"),
    Case("absent_registration", "absent",
         "How many FIRs were registered this month?", must_refuse=True),
    Case("absent_risk", "absent",
         "How many people have a criminal risk score above 80?",
         must_refuse=True),
    Case("bilingual_count", "bilingual", UR_MONTH),
    Case("bilingual_weapon", "bilingual", UR_WEAPON),
    Case("followup_base", "followup", "weapons recorded in Lahore"),
    Case("followup_next", "followup", "and Karachi?", follows="followup_base"),
]

_NUM = re.compile(r"\b\d[\d,]*\b")


@dataclass
class Outcome:
    case: Case
    seconds: float = 0.0
    status: str = ""
    text: str = ""
    events: int = 0
    asserted_figures: list[str] = field(default_factory=list)
    stated_coverage: bool = False
    error: str = ""

    @property
    def verdict(self) -> str:
        if self.error:
            return "ERROR"
        if self.case.must_refuse:
            # The failure here is asserting a figure the data cannot support.
            return "PASS" if not self.asserted_figures else "FAIL"
        if self.asserted_figures and not self.stated_coverage:
            return "FAIL"          # a number with no stated population
        return "PASS" if self.status else "FAIL"


def build_client():
    """The real app, with authentication and the data gateway substituted."""
    from fastapi.testclient import TestClient
    from src.main import app
    from src.auth.routes import get_current_user
    from src.data_gateway import get_gateway as real_get_gateway

    user_id = uuid.uuid4()

    class _User:
        id = user_id
        email = "infra-check@local"
        is_admin = False
        # An `investigator` with no case selected gets cases=[] by design:
        # conversation.prepare() grants a case list only to supervisor,
        # station-admin and platform-admin, because user prose must not be
        # able to widen its own scope. A supervisor is the realistic role
        # for corpus-wide questions.
        role = "supervisor"
        company_name = "Muhafiz"
        plan = "free"
        police_station = None

    sessions: dict[str, dict] = {}
    messages: list[dict] = []
    case_ids: list[str] = []

    class _Gateway:
        """Only the methods conversation.prepare() actually calls."""

        async def get_session(self, session_id):
            return sessions.get(str(session_id))

        async def create_session(self, session_id, uid, title,
                                 project_id=None, case_id=None):
            sessions[str(session_id)] = {
                "session_id": str(session_id), "user_id": str(uid),
                "case_id": case_id, "project_id": project_id, "title": title}
            return sessions[str(session_id)]

        async def get_cases(self, uid, role):
            return [{"case_id": c} for c in case_ids]

        async def get_attachments_for_session(self, session_id, include_text=False):
            return []

        async def get_session_history(self, session_id):
            return [m for m in messages if m["session_id"] == str(session_id)]

        async def get_user_context_profile(self, uid):
            return {"preferred_language": "auto"}

        async def save_message(self, session_id, role, content, **kwargs):
            messages.append({"session_id": str(session_id), "role": role,
                             "content": content})

        async def get_project(self, project_id):
            return None

        async def get_case(self, case_id):
            return {"case_id": case_id}

        async def check_case_access(self, case_id, uid, role):
            return True

    # The cases this supervisor can see, read from the evidence store so
    # the API test runs against the same population as everything else.
    import asyncpg
    async def _load_cases():
        conn = await asyncpg.connect(os.getenv("EVIDENCE_INGEST_DATABASE_URL"))
        for name in ("json", "jsonb"):
            await conn.set_type_codec(name, encoder=json.dumps,
                                      decoder=json.loads, schema="pg_catalog")
        rows = await conn.fetch(
            "SELECT DISTINCT unnest(case_ids) c FROM evidence.documents "
            "WHERE source_metadata->>'endpoint'='fir'")
        await conn.close()
        return [r["c"] for r in rows]
    case_ids.extend(asyncio.run(_load_cases()))

    async def _get_gateway():
        return _Gateway()

    import src.main as main_module
    main_module.get_gateway = _get_gateway
    for module in ("src.data_gateway", "src.data_gateway.selector"):
        try:
            __import__(module)
            setattr(sys.modules[module], "get_gateway", _get_gateway)
        except Exception:                               # noqa: BLE001
            pass

    app.dependency_overrides[get_current_user] = lambda: _User()
    app.dependency_overrides[real_get_gateway] = _get_gateway

    # Entered as a context manager ON PURPOSE. Without it, TestClient
    # spins a fresh portal -- and therefore a fresh event loop -- per
    # request. asyncpg pools are bound to the loop that created them, and
    # src/evidence/database.py caches its pools at module level, so the
    # second request reached a pool whose loop had gone:
    #
    #   InterfaceError: another operation is in progress
    #   ConnectionDoesNotExistError: connection closed mid-operation
    #   HTTP 503 "The evidence runtime is not ready"
    #
    # That is an artefact of driving the app this way, not a defect in it:
    # under uvicorn every request shares one loop. One portal here
    # reproduces that.
    client = TestClient(app)
    client.__enter__()
    return client, sessions


def ask(client, session_id: str, question: str) -> tuple[str, str, int]:
    """One turn through POST /api/chat, reading the SSE stream."""
    with client.stream("POST", "/api/chat",
                       json={"session_id": session_id, "message": question},
                       timeout=180) as response:
        if response.status_code != 200:
            body = response.read().decode("utf-8", "replace")[:200]
            raise RuntimeError(f"HTTP {response.status_code}: {body}")
        text, status, events = "", "", 0
        for line in response.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            events += 1
            try:
                event = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            if event.get("status"):
                status = event["status"]
            chunk = event.get("detail") or event.get("text") or ""
            if event.get("step") in {"response", "answer"} and chunk:
                text = chunk if len(chunk) > len(text) else text
        return text, status, events


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only")
    args = ap.parse_args()

    cases = [c for c in CASES if not args.only or args.only in (c.kind, c.name)]
    client, sessions = build_client()
    session_id = str(uuid.uuid4())
    outcomes: list[Outcome] = []

    print(f"driving POST /api/chat with {len(cases)} case(s)\n")
    for case in cases:
        outcome = Outcome(case=case)
        started = time.monotonic()
        try:
            text, status, events = ask(client, session_id, case.question)
            outcome.seconds = round(time.monotonic() - started, 1)
            outcome.text, outcome.status, outcome.events = text, status, events
            outcome.asserted_figures = _NUM.findall(text)[:5]
            outcome.stated_coverage = any(
                marker in text for marker in
                ("Documents checked", "جانچی گئی دستاویزات", "population",
                 "Partial evidence", "not been fully extracted"))
        except Exception as exc:                        # noqa: BLE001
            outcome.seconds = round(time.monotonic() - started, 1)
            outcome.error = f"{type(exc).__name__}: {str(exc)[:160]}"
        outcomes.append(outcome)
        print(f"  [{outcome.verdict:<5}] {case.kind:<12} {case.name:<20} "
              f"{outcome.seconds:6.1f}s status={outcome.status or '-'} "
              f"figures={outcome.asserted_figures or '-'}")
        if outcome.error:
            print(f"          {outcome.error}")
        elif outcome.text:
            print(f"          {' '.join(outcome.text.split())[:160]}")

    passed = sum(1 for o in outcomes if o.verdict == "PASS")
    print(f"\n{'=' * 64}")
    print(f"{passed}/{len(outcomes)} passed")
    for kind in sorted({c.kind for c in cases}):
        group = [o for o in outcomes if o.case.kind == kind]
        print(f"  {kind:<12} {sum(1 for o in group if o.verdict=='PASS')}/{len(group)}")
    print("\nA figure without a stated population counts as a failure, "
          "however plausible it looks.")


if __name__ == "__main__":
    main()
