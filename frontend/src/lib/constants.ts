export const LAST_SESSION_KEY = 'muhafiz_last_session';

// Same role floor the backend gates "All Cases" retrieval and cross-case
// tools (XGRAPH/XAGG/XNETWORK) on — orchestrator.py's
// _build_retrieval_where() / graph_retriever.CROSS_CASE_ROLES. Single
// source of truth here since Sidebar.tsx, ChatPanel.tsx, and ChatInput.tsx
// all need the same check and previously each kept their own copy.
export const ALL_CASES_ROLES = ['supervisor', 'station-admin', 'platform-admin'];
