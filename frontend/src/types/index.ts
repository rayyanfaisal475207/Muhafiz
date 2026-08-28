// ============================================================
// Shared TypeScript Types
// ============================================================

/** A single Server-Sent Event from the /chat endpoint */
export interface PipelineEvent {
  step: string;
  status: 'active' | 'done' | 'skipped' | 'error' | 'streaming' | 'waiting';
  detail?: string;
  thinking?: string;
  ms?: number;
  retry_num?: number;
  sources?: Source[];
  /** Deepest hop a GRAPH/GRAPH_HYBRID/XGRAPH traversal actually reached. */
  hop_count?: number;
  /** Compounded (multiplied-across-hops) confidence of that traversal —
   * degrades the further a connection is from the seed entity. Surfaced
   * so a multi-hop, weakly-confirmed connection isn't shown with the same
   * visual certainty as a direct, hop-0 one. */
  graph_confidence?: number;
}

/** Visual state of one step card in the pipeline panel */
export interface PipelineStep {
  name: string;
  label: string;
  status: 'waiting' | 'active' | 'done' | 'skipped' | 'error' | 'retry';
  detail?: string;
  ms?: number;
  retryNum?: number;
  hopCount?: number;
  graphConfidence?: number;
}

/** A source citation extracted from retrieval events */
export interface Source {
  filename: string;
  score?: number;
  file_id?: string;
  type?: string;
}

/** A single chat message (user or assistant) */
/**
 * Per-query degradation trace from the agent harness — what worked and what
 * failed while producing one answer.
 *
 * Built once per query by the supervisor's completion hook
 * (build_degradation_trace()), persisted on the assistant's message
 * (migration 019), and therefore restored on reload rather than lost with the
 * live SSE stream.
 *
 * `labels` carries PRE-RENDERED investigator-facing source names. Render those
 * verbatim — do NOT map the raw tool identifiers in `contributed_only` etc.
 * client-side. The canonical label map lives in the backend's contracts.py,
 * and a second copy over here is exactly the drift risk being avoided.
 */
export interface DegradationTrace {
  v: number;
  sub_agent_status: string;
  tools_used: string[];
  degraded_from: string[];
  /** Raw tool identifiers. For logic/keys only — never displayed. */
  contributed_only: string[];
  degraded_and_contributed: string[];
  degraded_only: string[];
  /** Display strings, already mapped by the backend. Render these. */
  labels: {
    contributed_only: string[];
    degraded_and_contributed: string[];
    degraded_only: string[];
  };
  caveats: string[];
  disclosure_rendered: boolean | null;
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  sources?: Source[];
  pipelineEvents?: PipelineEvent[];
  thinkingLogs?: string[];
  isStreaming?: boolean;
  /**
   * Undefined means NO TRACE RECORDED (legacy-path or pre-harness message) —
   * deliberately distinct from a trace showing a clean run. Renders nothing.
   */
  degradationTrace?: DegradationTrace;
}

/**
 * A file attached to ONE conversation from the chat composer.
 *
 * Deliberately not a knowledge-base document: attachments are never embedded,
 * never indexed, and never retrievable from another conversation. Ingestion
 * into the shared knowledge base is an admin function.
 */
export interface Attachment {
  attachment_id: string;
  session_id: string;
  filename: string;
  file_type?: string;
  file_size_bytes?: number;
  char_count?: number;
  status: 'ready' | 'failed';
  error_message?: string | null;
  created_at?: string;
}

/** A file being uploaded, before the server has replied. */
export interface PendingAttachment {
  tempId: string;
  filename: string;
  size: number;
  status: 'uploading' | 'failed';
  error?: string;
}

/** Canonical pipeline step order and labels */
export const PIPELINE_STEPS: Array<{ name: string; label: string }> = [
  { name: 'query_rewriter', label: 'Query Rewriter' },
  { name: 'router',         label: 'Router' },
  { name: 'retrieval',      label: 'Retrieval' },
  { name: 'reranker',       label: 'Re-ranker' },
  { name: 'evaluator',      label: 'Evaluator' },
  { name: 'response',       label: 'Response' },
  { name: 'memory',         label: 'Memory' },
];

// Human-readable labels for any pipeline step that can arrive in an SSE
// event, including the agent-harness steps the legacy PIPELINE_STEPS list
// above doesn't cover. Steps not listed here fall back to a title-cased
// version of their raw name, so a new backend step still renders sanely.
export const STEP_LABELS: Record<string, string> = {
  query_rewriter: 'Query Rewriter',
  router: 'Router',
  retrieval: 'Retrieval',
  reranker: 'Re-ranker',
  cross_reranker: 'Cross-Encoder Rerank',
  evaluator: 'Evaluator',
  web_search: 'Web Search',
  citation_validator: 'Citation Check',
  cross_case_finding: 'Cross-Case Finding',
  response: 'Response',
  file_generation: 'File Generation',
  title_generation: 'Title',
  memory: 'Memory',
  // Agent-harness steps
  supervisor: 'Supervisor',
  'supervisor:dispatch': 'Sub-Agent Dispatch',
  timeline_building: 'Timeline Building',
  data_quality: 'Data Quality',
  system: 'System',
};

export function stepLabel(name: string): string {
  if (STEP_LABELS[name]) return STEP_LABELS[name];
  return name
    .replace(/[:_]/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase());
}
