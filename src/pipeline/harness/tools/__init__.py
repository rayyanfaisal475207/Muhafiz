"""
Layer 1 — tool primitives.

[PRESERVE — design §1] Tools are NOT independently supervisor-routable. The
supervisor selects a sub-agent; a tool is only ever invoked from inside a
sub-agent's own composition logic. This is the mechanism that keeps case/role
scoping and cross-case structural separation enforceable — if the supervisor
could reach a tool directly, every enforcement point currently living inside a
specific call chain would need re-deriving at the supervisor level.

All seven are currently stubs (`stubs.py`) backed by deliberately messy fixture
evidence (`_fixtures.py`).
"""
