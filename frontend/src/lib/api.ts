// ============================================================
// API Layer — Axios Client & SSE Streams
// ============================================================

import axios, { AxiosError } from 'axios';
import type { Attachment, PipelineEvent } from '../types';

const BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000/api';

// ── Axios Instance ──────────────────────────────────────────────────────────
export const apiClient = axios.create({
  baseURL: BASE_URL,
  withCredentials: true,
});

// Request Interceptor: Attach CSRF Token
apiClient.interceptors.request.use((config) => {
  if (config.method && ['post', 'put', 'delete', 'patch'].includes(config.method.toLowerCase())) {
    const match = document.cookie.match(new RegExp('(^| )csrf_token=([^;]+)'));
    if (match) {
      config.headers['X-CSRF-Token'] = match[2];
    }
  }
  return config;
});

// Response Interceptor: Global 401 handler
apiClient.interceptors.response.use(
  (response) => response,
  (error: AxiosError) => {
    if (error.response && error.response.status === 401) {
      // Dispatch custom event caught by App.tsx or authStore
      window.dispatchEvent(new Event('auth:unauthorized'));
    }
    return Promise.reject(error);
  }
);

// ── SSE Streaming Chat ──────────────────────────────────────────────────────

// No chunk (not even a keepalive) for this long means the connection is
// stalled, not just a slow answer. The pipeline can involve several
// sequential LLM calls plus retries, so this is set well above any single
// call's expected latency — long enough not to false-positive on a
// legitimately slow-but-working response, short enough that a genuinely
// dead connection doesn't look "still working" forever.
// 150s: the harness sub-agents now emit per-phase events (retrieval →
// reranker → evaluator → response), so this timer resets on each phase and
// rarely approaches the limit — but the raised ceiling is a safety net for
// the one genuinely long gap (a single slow LLM generation call) so a
// working-but-slow answer isn't killed mid-flight.
const STREAM_STALL_TIMEOUT_MS = 150_000;

class StreamStallError extends Error {
  constructor() {
    super('Connection seems stalled — no response received in a while.');
    this.name = 'StreamStallError';
  }
}

async function readWithStallTimeout(
  reader: ReadableStreamDefaultReader<Uint8Array>,
): Promise<ReadableStreamReadResult<Uint8Array>> {
  let timer: ReturnType<typeof setTimeout>;
  const stall = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new StreamStallError()), STREAM_STALL_TIMEOUT_MS);
  });
  try {
    return await Promise.race([reader.read(), stall]);
  } finally {
    clearTimeout(timer!);
  }
}

export async function streamChat(
  sessionId: string,
  message: string,
  onEvent: (event: PipelineEvent) => void,
  signal?: AbortSignal,
  projectId?: string | null,
  caseId?: string | null,
  enableWebSearch?: boolean,
): Promise<void> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };

  const csrfMatch = document.cookie.match(new RegExp('(^| )csrf_token=([^;]+)'));
  if (csrfMatch) {
    headers['X-CSRF-Token'] = csrfMatch[2];
  }

  const bodyData: any = { session_id: sessionId, message };
  if (projectId) {
    bodyData.project_id = projectId;
  }
  if (caseId) {
    bodyData.case_id = caseId;
  }
  // Explicit per-query opt-in only — never inferred, never a fallback from
  // a failed RAG attempt. See src/pipeline/orchestrator.py's process_query().
  if (enableWebSearch) {
    bodyData.enable_web_search = true;
  }

  const response = await fetch(`${BASE_URL}/chat`, {
    method: 'POST',
    headers,
    body: JSON.stringify(bodyData),
    credentials: 'include', // Important for HttpOnly cookie
    signal,
  });

  if (!response.ok) {
    if (response.status === 401) window.dispatchEvent(new Event('auth:unauthorized'));
    const text = await response.text();
    throw new Error(`Chat request failed: ${response.status} ${text}`);
  }

  if (!response.body) throw new Error('No response body received');

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  try {
    while (true) {
      const { done, value } = await readWithStallTimeout(reader);
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split('\n\n');
      buffer = parts.pop() ?? '';

      for (const part of parts) {
        const line = part.trim();
        if (!line.startsWith('data: ')) continue;
        try {
          const event = JSON.parse(line.slice(6)) as PipelineEvent;
          onEvent(event);
        } catch (parseErr) {
          console.warn('streamChat: dropped malformed SSE chunk', line, parseErr);
        }
      }
    }
  } catch (err) {
    // A stall (or any other read failure) leaves the connection half-open —
    // cancel it explicitly rather than letting it linger.
    reader.cancel().catch(() => {});
    throw err;
  }

  if (buffer.trim().startsWith('data: ')) {
    try {
      const event = JSON.parse(buffer.trim().slice(6)) as PipelineEvent;
      onEvent(event);
    } catch (parseErr) {
      console.warn('streamChat: dropped malformed trailing SSE chunk', buffer.trim(), parseErr);
    }
  }
}

// ── Chat attachments ────────────────────────────────────────────────────────
// Files attached to ONE conversation. These are NOT knowledge-base documents:
// they are never embedded or indexed, and are visible only to this session.
// Knowledge-base ingestion lives in the admin app.

export async function uploadAttachment(sessionId: string, file: File): Promise<Attachment> {
  const form = new FormData();
  form.append('session_id', sessionId);
  form.append('file', file);
  const { data } = await apiClient.post<Attachment>('/attachments', form);
  return data;
}

export async function listAttachments(sessionId: string): Promise<Attachment[]> {
  const { data } = await apiClient.get<Attachment[]>('/attachments', {
    params: { session_id: sessionId },
  });
  return data;
}

export async function deleteAttachment(attachmentId: string): Promise<void> {
  await apiClient.delete(`/attachments/${attachmentId}`);
}

// ── Health ──────────────────────────────────────────────────────────────────
export async function getHealth(): Promise<Record<string, unknown>> {
  const { data } = await apiClient.get<Record<string, unknown>>('/health');
  return data;
}

