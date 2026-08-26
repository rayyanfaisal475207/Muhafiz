# ============================================================
# Shared helper: resolve a REAL admin identity for a script that performs
# reviewed graph mutations.
#
# WHY THIS EXISTS — a live-confirmed audit gap, not a hypothetical.
# scripts/collapse_same_document_duplicate_persons.py and its pass-2
# sibling both used a locally-minted `uuid.uuid4()` as the acting admin.
# That id exists in no `users` row, and `audit_logs.user_id` carries a
# foreign key to `users`, so EVERY audit write raised
# ForeignKeyViolationError and was swallowed by
# DirectGateway.log_audit_event()'s own try/except:
#
#     Key (user_id)=(587f6d2d-...) is not present in table "users".
#
# The graph mutation itself succeeded, so the run LOOKED fine. Measured
# consequence of one real run: 103 SAME_AS confirmations landed in the
# graph with ZERO audit records written. The original script's comment
# described this as "harmlessly logged and swallowed, but noisy" — that
# was wrong. README.md's own security section promises "Append-only audit
# log — admin actions, case-assignment changes, graph writes ... are
# recorded", and an unattributable bulk graph mutation in a police
# evidence system is a compliance gap, not noise.
#
# It also mattered beyond the audit table: graph_review.confirm_match()
# stamps `reviewed_by: str(admin.id)` onto the confirmed SAME_AS edge
# itself, so a fake id becomes permanent, unresolvable provenance on
# every edge it touches.
#
# THE RULE THIS ENFORCES: a script that mutates reviewed graph state must
# act as a real, named platform-admin, supplied explicitly. It must never
# invent an identity, and never silently pick one — attribution is the
# entire point of an audit record, so guessing who did it is no better
# than not recording it.
# ============================================================

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional


class AdminIdentityError(RuntimeError):
    """Raised when a real admin identity cannot be resolved. Fails LOUD —
    a mutation script must stop rather than fall back to an unattributable
    identity, which is exactly the failure this module exists to prevent."""


@dataclass(frozen=True)
class ScriptAdmin:
    """
    Minimal stand-in for the `User` object graph_review's endpoints expect
    (they only read `.id`). `id` is a real `uuid.UUID` because
    confirm_match()'s audit write casts it to ::UUID, and it corresponds
    to a real `users` row so the foreign key holds.
    """

    id: uuid.UUID
    email: str
    role: str


async def resolve_admin(email: Optional[str]) -> ScriptAdmin:
    """
    Look up `email` and return it as the acting admin, or raise.

    `email` is REQUIRED — passing None raises rather than defaulting to
    "the first platform-admin found". Two admins in the table make any
    automatic choice arbitrary, and an audit record naming the wrong
    person is worse than a loud failure telling the operator to say who
    they are.

    The role check is a real gate, not decoration: graph_review's own
    endpoints sit behind `require_role("supervisor")`, so a script acting
    outside the HTTP layer has to enforce the equivalent itself or it
    becomes a way to bypass that gate entirely.
    """
    if not email or not email.strip():
        raise AdminIdentityError(
            "No admin identity supplied. Re-run with --admin-email <email> naming the "
            "platform-admin performing this operation.\n"
            "This is required, not optional: the confirmation is stamped onto every edge "
            "it touches (reviewed_by) and written to the append-only audit log, so it must "
            "name a real, accountable person."
        )

    from src.data_gateway.selector import get_gateway

    gateway = await get_gateway()
    user = await gateway.get_user_by_email(email.strip())
    if not user:
        raise AdminIdentityError(
            f"No user found with email {email!r}. The acting admin must already exist in "
            "the users table — this script does not create one."
        )

    role = (user.get("role") or "").strip()
    if role not in ("platform-admin", "supervisor", "station-admin"):
        raise AdminIdentityError(
            f"User {email!r} has role {role!r}, which cannot review graph matches. "
            "graph_review's own endpoints require 'supervisor' or higher; a script must "
            "enforce the same bar rather than bypass it."
        )

    try:
        admin_id = uuid.UUID(str(user["id"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise AdminIdentityError(
            f"User {email!r} has an unusable id {user.get('id')!r}: {exc}"
        ) from exc

    return ScriptAdmin(id=admin_id, email=user.get("email") or email, role=role)
