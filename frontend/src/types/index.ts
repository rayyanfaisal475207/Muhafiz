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
export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  sources?: Source[];
  pipelineEvents?: PipelineEvent[];
  thinkingLogs?: string[];
  isStreaming?: boolean;
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
