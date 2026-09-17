// ============================================================
// caseLabel — one source of truth for how a case is named in the UI.
//
// Three places need to name a case and previously each did it its own
// way (or not at all): CaseSelector's combobox rows, the chat header's
// scope line, and the per-session badges in Chat History. A case is
// shown by its FIR number when it has one and its raw case_id otherwise,
// so the same case never appears under two different names depending on
// which part of the UI you are looking at.
// ============================================================

import type { Case } from '../store/caseStore';
import { ALL_CASES_ROLES } from './constants';

/** How a single case is written wherever it appears. */
export function caseLabel(c: Case): string {
  return c.fir_number || c.case_id;
}

/**
 * What to call the "no specific case" scope for this user.
 *
 * The distinction is not cosmetic — it mirrors the backend's own
 * retrieval scoping (orchestrator.py's `_build_retrieval_where`).
 * Supervisor and above with no case selected search every case's
 * evidence ("All Cases"); an investigator in the same state searches
 * only the shared reference material, so calling that "All Cases"
 * would promise access they do not have.
 */
export function allCasesLabel(role: string | undefined): string {
  return ALL_CASES_ROLES.includes(role || '') ? 'All Cases' : 'No Case';
}

/**
 * The scope line shown in the chat header and above Chat History:
 * the active case's name, or the all-cases/no-case label.
 *
 * `cases` may not have loaded yet (or may omit a case the session was
 * started under), so an unknown id falls back to the id itself rather
 * than rendering an empty string.
 */
export function scopeLabel(
  activeCaseId: string | null | undefined,
  cases: Case[],
  role: string | undefined,
): string {
  if (!activeCaseId) return allCasesLabel(role);
  const found = cases.find((c) => c.case_id === activeCaseId);
  return found ? caseLabel(found) : activeCaseId;
}
