"""
Shared test fixtures.

Design rules for this suite:
  * No network. No real database, no real LLM. Every external
    boundary is faked, so the suite runs anywhere in seconds.
  * Tests assert behaviour that broke in production, not implementation
    detail. Each regression test names the bug it guards against.
"""
import sys
import uuid
from pathlib import Path
from typing import Optional

import pytest

# Make `src` importable when pytest is run from the repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── Fake data gateway ─────────────────────────────────────────────────────────

class FakeGateway:
    """
    In-memory stand-in for DirectGateway / RestGateway.

    Implements the slice of the DataGateway protocol the pipeline and API
    actually use, and records calls so tests can assert on persistence
    behaviour (e.g. "was the session created with an owner?").
    """

    def __init__(self):
        self.sessions: dict[str, dict] = {}
        self.messages: list[dict] = []
        self.runs: list[dict] = []
        self.steps: list[dict] = []
        self.files: dict[str, dict] = {}
        self.projects: dict[str, dict] = {}
        self.project_memory: dict[str, str] = {}
        self.profiles: dict[str, dict] = {}
        self.users: dict[str, dict] = {}
        self.chunks: list[dict] = []
        # Per-conversation attachments. Deliberately a DIFFERENT store from
        # `chunks`: if a change ever makes an attachment land in `chunks`, the
        # separation tests fail — which is the whole point.
        self.attachments: list[dict] = []
        self.errors: list[dict] = []
        self.jobs: list[dict] = []
        self.police_reference_data: list[dict] = []
        self.mcp_tool_calls: list[dict] = []
        self.audit_log: list[dict] = []
        self.case_assignments: dict[str, list[dict]] = {}
        # Case ids listed here fail check_case_access(), everything else is
        # allowed by default — tests opt IN to denial rather than every
        # other test needing to opt out of a default-deny.
        self.denied_case_ids: set[str] = set()
        self.cases: dict[str, dict] = {}

    # ── Audit log (Phase 7) ──
    async def log_audit_event(self, event_type: str, details: dict = None, user_id: str = None, case_id: str = None) -> None:
        self.audit_log.append({
            "event_type": event_type, "details": details, "user_id": user_id, "case_id": case_id,
        })

    # ── Case access (Phase 7 RBAC/ABAC) ──
    async def check_case_access(self, case_id: str, user_id: str, user_role: str, min_role: str = None) -> bool:
        if case_id in self.denied_case_ids:
            return False
        if user_role == "platform-admin":
            return True
        # Phase 5, Module 5.4: this used to short-circuit to True whenever
        # min_role was None ("any assignment" threshold), without actually
        # consulting case_assignments — fine while every real caller of
        # that threshold happened to set up an assignment anyway, but it
        # meant this fake couldn't distinguish "assigned" from "not
        # assigned" for a station-admin's case-scoped file download (Module
        # 5.4's new check). Now always requires a real per-case
        # case_assignments row (matching DirectGateway.check_case_access),
        # then additionally enforces the role hierarchy when min_role is set.
        assignments = self.case_assignments.get(case_id, [])
        assignment = next((a for a in assignments if a["user_id"] == str(user_id)), None)
        if assignment is None:
            return False
        if min_role is None:
            return True
        roles = ["investigator", "supervisor", "station-admin", "platform-admin"]
        try:
            return roles.index(assignment["role"]) >= roles.index(min_role)
        except ValueError:
            return False

    # ── Cases (Phase 5, Module 5.1 test support) ──
    _CASE_DEFAULTS = {"created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-01T00:00:00"}

    async def get_case(self, case_id: str) -> Optional[dict]:
        case = self.cases.get(case_id)
        return {**self._CASE_DEFAULTS, **case} if case else None

    async def get_cases(self, user_id: str = None, user_role: str = None) -> list[dict]:
        return [{**self._CASE_DEFAULTS, **c} for c in self.cases.values()]

    async def create_case(self, data: dict) -> Optional[dict]:
        case_id = data["case_id"]
        self.cases[case_id] = dict(data)
        return {**self._CASE_DEFAULTS, **self.cases[case_id]}

    async def update_case(self, case_id: str, data: dict) -> Optional[dict]:
        if case_id not in self.cases:
            return None
        self.cases[case_id].update(data)
        return {**self._CASE_DEFAULTS, **self.cases[case_id]}

    async def delete_case(self, case_id: str) -> None:
        self.cases.pop(case_id, None)

    # ── Case assignments (Phase 7 RBAC) ──
    async def assign_user_to_case(self, case_id: str, user_id: str, role: str) -> None:
        assignments = self.case_assignments.setdefault(case_id, [])
        existing = next((a for a in assignments if a["user_id"] == str(user_id)), None)
        if existing:
            existing["role"] = role
        else:
            user = self.users.get(str(user_id))
            assignments.append({"user_id": str(user_id), "email": user["email"] if user else None, "role": role})

    async def get_case_assignments(self, case_id: str) -> list[dict]:
        return list(self.case_assignments.get(case_id, []))

    async def unassign_user_from_case(self, case_id: str, user_id: str) -> None:
        assignments = self.case_assignments.get(case_id, [])
        self.case_assignments[case_id] = [a for a in assignments if a["user_id"] != str(user_id)]

    # ── Users / profile ──
    async def get_user_by_id(self, user_id):
        return self.users.get(str(user_id))

    async def get_user_by_email(self, email):
        return next((u for u in self.users.values() if u["email"] == email), None)

    async def create_user(self, user_data: dict) -> dict:
        new_id = str(uuid.uuid4())
        role = user_data.get("role", "investigator")
        record = {
            "id": new_id,
            "email": user_data["email"],
            "password_hash": user_data.get("password_hash", ""),
            "role": role,
            "is_admin": role == "platform-admin",
            "company_name": user_data.get("company_name"),
            "plan": "free",
        }
        self.users[new_id] = record
        return record

    async def get_all_users(self, limit=50, offset=0):
        return list(self.users.values())[offset:offset + limit]

    async def get_user_context_profile(self, user_id) -> dict:
        return self.profiles.get(str(user_id), {
            "id": str(user_id), "context_text": "", "preferred_language": "english", "llm_mode": "cloud",
        })

    async def update_user_context_profile(self, user_id, data) -> dict:
        prof = {"id": str(user_id), **data}
        self.profiles[str(user_id)] = prof
        return prof

    # ── Sessions / messages ──
    async def create_session(self, session_id, user_id, title, project_id=None, case_id=None) -> None:
        self.sessions[str(session_id)] = {
            "session_id": str(session_id),
            "user_id": str(user_id) if user_id else None,
            "project_id": str(project_id) if project_id else None,
            "case_id": str(case_id) if case_id else None,
            "title": title,
        }

    async def get_session(self, session_id) -> Optional[dict]:
        return self.sessions.get(str(session_id))

    async def get_sessions_for_user(self, user_id, project_id=None, case_id=None) -> list[dict]:
        results = [s for s in self.sessions.values() if s["user_id"] == str(user_id)]
        if project_id:
            results = [s for s in results if s.get("project_id") == str(project_id)]
        if case_id:
            results = [s for s in results if s.get("case_id") == str(case_id)]
        return results

    async def update_session_title(self, session_id, title) -> None:
        if str(session_id) in self.sessions:
            self.sessions[str(session_id)]["title"] = title

    async def delete_session(self, session_id) -> None:
        self.sessions.pop(str(session_id), None)

    async def save_message(self, session_id, role, content) -> None:
        self.messages.append({
            "message_id": str(uuid.uuid4()),
            "session_id": str(session_id), "role": role, "content": content,
        })

    async def get_session_history(self, session_id) -> list[dict]:
        return [m for m in self.messages if m["session_id"] == str(session_id)]

    async def update_message_citations(self, session_id, response_text, unverified) -> None:
        pass

    # ── Pipeline logging ──
    async def create_run(self, session_id, user_message) -> str:
        run_id = str(uuid.uuid4())
        self.runs.append({"run_id": run_id, "session_id": str(session_id), "original_query": user_message})
        return run_id

    async def update_run(self, run_id, **kwargs) -> None:
        pass

    async def log_step(self, run_id, step_name, step_order, status, duration_ms=None,
                       input_summary=None, output_summary=None) -> None:
        self.steps.append({"run_id": run_id, "step_name": step_name, "status": status})

    async def log_mcp_tool_call(self, run_id, mcp_server, tool_name, status,
                                input_params=None, output_summary=None,
                                duration_ms=None, rejected_by_role=False) -> None:
        self.mcp_tool_calls.append({
            "run_id": run_id, "mcp_server": mcp_server, "tool_name": tool_name,
            "status": status, "input_params": input_params,
            "output_summary": output_summary, "duration_ms": duration_ms,
            "rejected_by_role": rejected_by_role,
        })

    # ── Projects ──
    async def get_project(self, project_id):
        return self.projects.get(str(project_id))

    async def get_projects_for_user(self, user_id):
        return [p for p in self.projects.values() if p["user_id"] == str(user_id)]

    async def create_project(self, data):
        pid = str(uuid.uuid4())
        proj = {"id": pid, **data}
        self.projects[pid] = proj
        return proj

    async def update_project(self, project_id, data):
        if str(project_id) not in self.projects:
            return None
        self.projects[str(project_id)].update(data)
        return self.projects[str(project_id)]

    async def delete_project(self, project_id) -> None:
        self.projects.pop(str(project_id), None)

    async def get_project_context(self, project_id):
        proj = self.projects.get(str(project_id))
        return proj.get("domain_context") if proj else None

    async def get_project_memory(self, project_id):
        text = self.project_memory.get(str(project_id))
        return {"project_id": str(project_id), "summary_text": text} if text else None

    async def upsert_project_memory(self, project_id, summary_text) -> None:
        self.project_memory[str(project_id)] = summary_text

    # ── Files ──
    async def log_generated_file(self, file_data: dict) -> str:
        file_id = str(uuid.uuid4())
        self.files[file_id] = {"file_id": file_id, **file_data}
        return file_id

    async def get_generated_file(self, file_id) -> Optional[dict]:
        return self.files.get(str(file_id))

    async def get_generated_files(self, limit=50, offset=0) -> list[dict]:
        return list(self.files.values())[offset:offset + limit]

    async def delete_generated_file(self, file_id) -> Optional[dict]:
        return self.files.pop(str(file_id), None)

    # ── Retrieval / misc ──
    async def query_police_reference_data(self, category=None, subject=None, section_ref=None) -> list[dict]:
        if not (category or subject or section_ref):
            return []
        results = self.police_reference_data
        if category:
            results = [r for r in results if category.lower() in r.get("category", "").lower()]
        if subject:
            results = [r for r in results if subject.lower() in r.get("subject", "").lower()]
        if section_ref:
            results = [r for r in results if section_ref.lower() in (r.get("section_ref") or "").lower()]
        return results

    async def delete_document_records(self, source_file) -> None:
        pass

    async def get_ingested_files_summary(self, project_id=None) -> list[dict]:
        return []

    async def get_system_metrics(self) -> dict:
        return {"total_runs": len(self.runs), "route_metrics": {}, "table_stats": []}

    async def get_runs(self, limit=50, offset=0, route_filter=None) -> list[dict]:
        return self.runs

    async def get_run_steps(self, run_id) -> list[dict]:
        return [s for s in self.steps if s["run_id"] == str(run_id)]

    async def get_mcp_calls(self, limit=50, offset=0) -> list[dict]:
        return []

    # ── Chat attachments (never the knowledge base) ──
    async def create_attachment(self, data: dict) -> dict:
        record = {"attachment_id": str(uuid.uuid4()), **data}
        self.attachments.append(record)
        return record

    async def get_attachments_for_session(self, session_id: str, include_text: bool = False) -> list[dict]:
        rows = [a for a in self.attachments if str(a["session_id"]) == str(session_id)]
        if include_text:
            return rows
        return [{k: v for k, v in a.items() if k != "extracted_text"} for a in rows]

    async def get_attachment(self, attachment_id: str) -> Optional[dict]:
        return next((a for a in self.attachments if a["attachment_id"] == str(attachment_id)), None)

    async def delete_attachment(self, attachment_id: str) -> None:
        self.attachments = [a for a in self.attachments if a["attachment_id"] != str(attachment_id)]

    # ── Observability ──
    async def log_error(self, record: dict) -> None:
        self.errors.append(record)

    async def get_errors(self, limit: int = 100, offset: int = 0, **kwargs) -> list[dict]:
        return self.errors[offset:offset + limit]

    async def get_error_facets(self) -> dict:
        return {"modules": [], "error_types": [], "severities": []}

    async def get_errors_since(self, since: str) -> list[dict]:
        return self.errors

    async def create_ingestion_job(self, data: dict) -> str:
        job_id = str(uuid.uuid4())
        self.jobs.append({"job_id": job_id, **data})
        return job_id

    async def update_ingestion_job(self, job_id: str, data: dict) -> None:
        for job in self.jobs:
            if job["job_id"] == str(job_id):
                job.update(data)

    async def get_ingestion_jobs(self, limit: int = 50, offset: int = 0) -> list[dict]:
        return self.jobs[offset:offset + limit]

    async def get_kb_stats(self) -> dict:
        return {"total_chunks": len(self.chunks), "total_documents": 0, "documents": []}

    async def get_runs_since(self, since: str) -> list[dict]:
        return self.runs

    async def get_step_latencies_since(self, since: str) -> list[dict]:
        return self.steps

    async def log_document(self, doc_id: str, filename: str, doc_type: str = None,
                           chunk_count: int = None, is_global: bool = False) -> None:
        pass


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """
    Hard guard: this suite must never touch the network. Without it, a missed
    patch quietly falls through to the real Postgres / LLM APIs — slow, flaky,
    and it mutates production data.
    """
    import socket

    real_socket = socket.socket
    _LOOPBACK = {"127.0.0.1", "::1", "localhost"}

    def _is_loopback(address) -> bool:
        # asyncio's event loop uses a loopback socketpair on Windows — allow it.
        if isinstance(address, tuple) and address:
            return str(address[0]) in _LOOPBACK
        return False

    class _GuardedSocket(real_socket):
        def connect(self, address, *args, **kwargs):
            if not _is_loopback(address):
                raise RuntimeError(
                    "Network access is disabled in tests — a boundary is unpatched. "
                    f"Attempted connection to {address!r}"
                )
            return super().connect(address, *args, **kwargs)

        def connect_ex(self, address, *args, **kwargs):
            if not _is_loopback(address):
                raise RuntimeError(
                    "Network access is disabled in tests — a boundary is unpatched. "
                    f"Attempted connection to {address!r}"
                )
            return super().connect_ex(address, *args, **kwargs)

    monkeypatch.setattr(socket, "socket", _GuardedSocket)


@pytest.fixture(autouse=True)
def reset_gateway_singleton():
    """
    get_gateway() caches its backend in a module global. Without resetting it,
    a real gateway constructed by one test leaks into every later test.
    """
    import src.data_gateway.selector as selector

    selector._gateway_instance = None
    yield
    selector._gateway_instance = None


@pytest.fixture
def gateway():
    return FakeGateway()


@pytest.fixture
def user_id():
    return str(uuid.uuid4())


@pytest.fixture
def session_id():
    return str(uuid.uuid4())


@pytest.fixture
def patched_gateway(monkeypatch, gateway):
    """Force every get_gateway() call in the app to return the fake gateway."""
    async def _get_gateway():
        return gateway

    for module in (
        "src.data_gateway.selector",
        "src.data_gateway",
        "src.pipeline.orchestrator",
        "src.memory.conversation",
        "src.api.sessions",
        "src.api.projects",
        "src.api.profile",
        "src.api.admin",
        "src.api.attachments",
        "src.main",
        "src.pipeline.title_generator",
    ):
        try:
            monkeypatch.setattr(f"{module}.get_gateway", _get_gateway, raising=False)
        except (AttributeError, ImportError):
            pass
    return gateway


# ── Fake LLM ──────────────────────────────────────────────────────────────────

class FakeLLM:
    """
    Scriptable stand-in for src.llm.client.

    `responses` maps a substring of the system prompt to the reply that should
    be returned, so a single fixture can drive rewriter/router/evaluator/response
    calls in one pipeline run.
    """

    def __init__(self):
        self.responses: dict[str, str] = {}
        self.default = "OK"
        self.calls: list[dict] = []

    def set(self, prompt_contains: str, response: str):
        self.responses[prompt_contains] = response

    def _resolve(self, system_prompt: str) -> str:
        for needle, response in self.responses.items():
            if needle.lower() in (system_prompt or "").lower():
                return response
        return self.default

    async def call_llm(self, system_prompt, user_message, **kwargs):
        self.calls.append({"system": system_prompt, "user": user_message, **kwargs})
        return self._resolve(system_prompt)

    async def stream_llm(self, system_prompt, user_message, **kwargs):
        self.calls.append({"system": system_prompt, "user": user_message, "stream": True, **kwargs})
        for token in self._resolve(system_prompt).split(" "):
            yield token + " "


@pytest.fixture
def fake_llm():
    return FakeLLM()
