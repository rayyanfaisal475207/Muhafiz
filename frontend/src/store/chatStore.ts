// ============================================================
// Chat Store — Zustand
// Manages all state for the chat interface:
//   - Session ID
//   - Message history
//   - Live pipeline events for the current response
//   - Streaming state
// ============================================================

import { create } from 'zustand';
import { generateSessionId } from '../lib/utils';
import {
  streamChat, apiClient,
  uploadAttachment, listAttachments, deleteAttachment,
} from '../lib/api';
import type {
  ChatMessage, PipelineEvent, Source, PipelineStep,
  Attachment, PendingAttachment, DegradationTrace,
} from '../types';
import { useProjectStore } from './projectStore';
import { useCaseStore } from './caseStore';

import { stepLabel } from '../types';

// ── Helpers ──────────────────────────────────────────────────────────────────

// Start empty: steps are appended as their SSE events arrive (see
// applyEventToSteps), so the panel shows exactly the steps the pipeline
// actually ran — whether that's the legacy orchestrator's 7 named steps or
// the agent-harness's Supervisor → Dispatch → Response — each lighting up
// live in real execution order instead of a frozen pre-seeded list.
// PIPELINE_STEPS is still used by the idle-state preview in PipelinePanel.
function buildInitialSteps(): PipelineStep[] {
  return [];
}

function statusForEvent(event: PipelineEvent, prev: PipelineStep['status']): PipelineStep['status'] {
  let status: PipelineStep['status'] = prev;
  // 'streaming' is the response step actively producing tokens — show it as
  // active (spinner), not stuck on 'waiting', so Response animates before it
  // resolves to done.
  if (event.status === 'active' || event.status === 'streaming') status = 'active';
  else if (event.status === 'done') status = 'done';
  else if (event.status === 'skipped') status = 'skipped';
  else if (event.status === 'error') status = 'error';
  // Retry: evaluator with retry_num > 0 and status != done
  if (
    event.step === 'evaluator' &&
    event.status === 'done' &&
    event.retry_num !== undefined &&
    event.retry_num > 0
  ) {
    status = 'retry';
  }
  return status;
}

function applyEventToSteps(
  steps: PipelineStep[],
  event: PipelineEvent,
): PipelineStep[] {
  const known = steps.some((s) => s.name === event.step);

  // A step that isn't in the pre-seeded legacy list (e.g. the agent-harness
  // 'supervisor' / 'supervisor:dispatch' steps, or any future backend step)
  // is appended in arrival order so the trace panel lights it up live rather
  // than dropping the event on the floor. This is what makes the harness
  // path show progressive steps instead of a frozen panel.
  if (!known) {
    return [
      ...steps,
      {
        name: event.step,
        label: stepLabel(event.step),
        status: statusForEvent(event, 'waiting'),
        detail: event.detail,
        ms: event.ms,
        retryNum: event.retry_num,
        hopCount: event.hop_count,
        graphConfidence: event.graph_confidence,
      },
    ];
  }

  return steps.map((step) => {
    if (step.name !== event.step) return step;
    return {
      ...step,
      status: statusForEvent(event, step.status),
      detail: event.detail ?? step.detail,
      ms: event.ms ?? step.ms,
      retryNum: event.retry_num,
      hopCount: event.hop_count ?? step.hopCount,
      graphConfidence: event.graph_confidence ?? step.graphConfidence,
    };
  });
}

function extractSources(events: PipelineEvent[]): Source[] {
  const sources: Source[] = [];
  for (const e of events) {
    if ((e.step === 'retrieval' || e.step === 'web_search') && e.status === 'done' && (e as any).sources) {
      sources.push(...(e as any).sources);
    }
  }
  return sources;
}

/**
 * The harness supervisor attaches the per-query degradation trace to its
 * completion event. Taking the LAST one is deliberate: the payload is built
 * once per sub-agent completion, so a single query yields one — but if the
 * supervisor ever dispatches more than once, the final result is the one that
 * describes the answer actually delivered.
 *
 * Returns undefined when no event carried a trace: legacy-orchestrator
 * queries produce none, and that must stay distinguishable from a clean run.
 */
function extractDegradationTrace(events: PipelineEvent[]): DegradationTrace | undefined {
  let found: DegradationTrace | undefined;
  for (const e of events) {
    const trace = (e as any).trace;
    if (trace) found = trace as DegradationTrace;
  }
  return found;
}

// ── Store Interface ───────────────────────────────────────────────────────────

interface ChatState {
  sessionId: string;
  messages: ChatMessage[];
  currentSteps: PipelineStep[];
  currentEvents: PipelineEvent[];
  currentSources: Source[];
  isStreaming: boolean;
  error: string | null;

  /** Files attached to THIS conversation (not knowledge-base documents). */
  attachments: Attachment[];
  pendingAttachments: PendingAttachment[];

  /**
   * Explicit per-query web-search opt-in (a UI toggle the user sets before
   * sending). Never inferred from message content and never triggered
   * automatically when RAG retrieval comes up empty — that automatic
   * fallback was removed. Read once at send time by sendMessage(), not
   * "sticky" across messages by design: it resets after each send so a web
   * search is never silently reused for a later, unrelated question.
   */
  webSearchEnabled: boolean;
  setWebSearchEnabled: (enabled: boolean) => void;

  // Actions
  newSession: () => void;
  /** Full reset for logout — same as newSession() plus the web-search toggle. */
  reset: () => void;
  loadSession: (id: string) => Promise<void>;
  sendMessage: (text: string) => Promise<void>;

  attachFile: (file: File) => Promise<void>;
  removeAttachment: (attachmentId: string) => Promise<void>;
  dismissPending: (tempId: string) => void;

  clearError: () => void;
}

// ── Store ─────────────────────────────────────────────────────────────────────

// AbortController for the in-flight chat stream. Kept outside the store so
// switching sessions / starting a new chat can cancel the previous stream
// instead of letting it keep writing into the wrong session's state.
let activeStreamController: AbortController | null = null;

function abortActiveStream() {
  if (activeStreamController) {
    activeStreamController.abort();
    activeStreamController = null;
  }
}

export const useChatStore = create<ChatState>((set, get) => ({
  sessionId: generateSessionId(),
  messages: [],
  currentSteps: buildInitialSteps(),
  currentEvents: [],
  currentSources: [],
  isStreaming: false,
  error: null,
  attachments: [],
  pendingAttachments: [],
  webSearchEnabled: false,
  setWebSearchEnabled: (enabled: boolean) => set({ webSearchEnabled: enabled }),

  newSession: () => {
    abortActiveStream();
    set({
      sessionId: generateSessionId(),
      messages: [],
      currentSteps: buildInitialSteps(),
      currentEvents: [],
      currentSources: [],
      isStreaming: false,
      error: null,
      attachments: [],
      pendingAttachments: [],
    });
  },

  reset: () => {
    get().newSession();
    set({ webSearchEnabled: false });
  },

  loadSession: async (id: string) => {
    abortActiveStream();
    try {
      set({
        sessionId: id,
        isStreaming: false,
        messages: [],
        currentSteps: buildInitialSteps(),
        currentEvents: [],
        currentSources: [],
        error: null,
        attachments: [],
        pendingAttachments: [],
      });
      const { data } = await apiClient.get<{
        history: { role: string; content: string; degradation_trace?: DegradationTrace | null }[]
      }>(`/sessions/${id}`);
      // Ignore the response if the user already switched somewhere else
      if (get().sessionId !== id) return;
      const messages: ChatMessage[] = data.history.map((h) => ({
        id: crypto.randomUUID(),

        role: h.role as 'user' | 'assistant',
        content: h.content,
        sources: [],
        thinkingLogs: [],
        isStreaming: false,
        // Migration 019: restored from the DB rather than reset to nothing.
        // `sources`/`thinkingLogs` above are still dropped on reload because
        // they are not persisted anywhere — the degradation trace is the one
        // piece of per-query transparency that now survives a refresh.
        // `undefined` (no trace recorded: a legacy-path or pre-harness
        // message) is deliberately distinct from an empty trace.
        degradationTrace: h.degradation_trace ?? undefined,
      }));
      set({ messages });

      // Attachments stay with the conversation, so restore them alongside it.
      try {
        const attachments = await listAttachments(id);
        if (get().sessionId === id) set({ attachments });
      } catch {
        // A missing attachments table (migration not yet applied) must not
        // stop the conversation itself from loading.
      }
    } catch (err) {
      if (get().sessionId === id) {
        set({ error: 'Failed to load session history' });
      }
    }
  },

  attachFile: async (file: File) => {
    const { sessionId } = get();
    const tempId = crypto.randomUUID();

    set((state) => ({
      pendingAttachments: [
        ...state.pendingAttachments,
        { tempId, filename: file.name, size: file.size, status: 'uploading' as const },
      ],
    }));

    try {
      const attachment = await uploadAttachment(sessionId, file);
      // The session may have changed while the file was uploading.
      if (get().sessionId !== sessionId) return;
      set((state) => ({
        attachments: [...state.attachments, attachment],
        pendingAttachments: state.pendingAttachments.filter((p) => p.tempId !== tempId),
      }));
    } catch (err: unknown) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        'Upload failed';
      // Keep the chip, marked failed — a silently vanishing file is worse
      // than a visible error.
      set((state) => ({
        pendingAttachments: state.pendingAttachments.map((p) =>
          p.tempId === tempId ? { ...p, status: 'failed' as const, error: detail } : p,
        ),
      }));
    }
  },

  removeAttachment: async (attachmentId: string) => {
    const previous = get().attachments;
    set({ attachments: previous.filter((a) => a.attachment_id !== attachmentId) });
    try {
      await deleteAttachment(attachmentId);
    } catch {
      set({ attachments: previous });  // put it back if the delete failed
    }
  },

  dismissPending: (tempId: string) => {
    set((state) => ({
      pendingAttachments: state.pendingAttachments.filter((p) => p.tempId !== tempId),
    }));
  },


  clearError: () => set({ error: null }),

  sendMessage: async (text: string) => {
    const { sessionId, webSearchEnabled } = get();
    // Read-once, per-message: resets immediately so an explicit web-search
    // opt-in never silently carries over to the next, unrelated question.
    set({ webSearchEnabled: false });

    // Add user message immediately
    const userMsg: ChatMessage = {
      id: crypto.randomUUID(),
      role: 'user',
      content: text,
    };

    // If this is the first message in the session, add it to the sidebar
    const isFirstMessage = get().messages.length === 0;
    if (isFirstMessage) {
      import('./sessionStore').then(({ useSessionStore }) => {
        const activeProjectId = useProjectStore.getState().activeProjectId;
        const activeCaseId = useCaseStore.getState().activeCaseId;
        useSessionStore.getState().addSessionOptimistic({
          session_id: sessionId,
          title: text.split(' ').slice(0, 5).join(' ') + '...',
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        }, activeProjectId, activeCaseId);
      });
    }

    // Add a placeholder assistant message that we'll fill in

    const assistantMsgId = crypto.randomUUID();
    const assistantMsg: ChatMessage = {
      id: assistantMsgId,
      role: 'assistant',
      content: '',
      sources: [],
      thinkingLogs: [],
      isStreaming: true,
    };

    set((state) => ({
      // A prior in-flight assistant message (rapid double-send) is about to
      // be abandoned by the abortActiveStream() call below. Its own stream
      // handler will short-circuit on the abort and never reach the code
      // that clears isStreaming, so it must be closed out here instead —
      // otherwise it's stuck showing a spinner forever.
      messages: [
        ...state.messages.map((m) =>
          m.isStreaming
            ? { ...m, isStreaming: false, content: m.content || '⚠️ Interrupted by a new message.' }
            : m,
        ),
        userMsg,
        assistantMsg,
      ],
      currentSteps: buildInitialSteps(),
      currentEvents: [],
      currentSources: [],
      isStreaming: true,
      error: null,
    }));

    let accumulatedResponse = '';
    const accumulatedEvents: PipelineEvent[] = [];

    abortActiveStream();
    const controller = new AbortController();
    activeStreamController = controller;
    // Only mutate state while this stream is still the active one for this
    // session — a session switch mid-stream must not leak into the new view.
    const isCurrent = () => !controller.signal.aborted && get().sessionId === sessionId;

    try {
      const activeProjectId = useProjectStore.getState().activeProjectId;
      const activeCaseId = useCaseStore.getState().activeCaseId;
      await streamChat(sessionId, text, (event: PipelineEvent) => {
        // Token events are transient — keeping hundreds of them per answer
        // in pipelineEvents bloats memory for zero UI value.
        if (!(event.step === 'response' && event.status === 'streaming')) {
          accumulatedEvents.push(event);
        }
        if (!isCurrent()) return;

        if (event.step === 'title_generation' && event.status === 'done' && event.detail) {
          import('./sessionStore').then(({ useSessionStore }) => {
            useSessionStore.getState().updateSessionTitleOptimistic(sessionId, event.detail!);
          });
        } else if (event.step === 'response' && event.status === 'streaming' && event.detail) {
          // Streaming token — append to the assistant message content only.
          // Tokens are NOT pushed into currentEvents: that array feeds the
          // pipeline panel and grew by one entry per token, forcing extra
          // recomputation across the tree on every token.
          accumulatedResponse += event.detail;
          set((state) => ({
            messages: state.messages.map((m) =>
              m.id === assistantMsgId
                ? { ...m, content: accumulatedResponse }
                : m,
            ),
          }));
        } else {
          // Pipeline step event — update the step card AND attach it to the
          // message, so the inline generation status can show the live phase.
          // Only step events land here (tokens are handled above), so this
          // stays at a few dozen entries per answer.
          set((state) => ({
            currentSteps: applyEventToSteps(state.currentSteps, event),
            currentEvents: [...state.currentEvents, event],
            messages: state.messages.map((m) =>
              m.id === assistantMsgId
                ? {
                    ...m,
                    pipelineEvents: [...(m.pipelineEvents || []), event],
                    thinkingLogs: event.detail && event.detail.trim().length > 0
                      ? [...(m.thinkingLogs || []), event.detail]
                      : m.thinkingLogs
                  }
                : m,
            ),
          }));
        }
      }, controller.signal, activeProjectId, activeCaseId, webSearchEnabled);

      if (!isCurrent()) return;

      // Extract any sources from events
      const sources = extractSources(accumulatedEvents);
      // Shown immediately from the live stream; the same payload is persisted
      // server-side on the assistant message, so it survives a reload too.
      const degradationTrace = extractDegradationTrace(accumulatedEvents);

      // Finalize the assistant message
      set((state) => ({
        messages: state.messages.map((m) =>
          m.id === assistantMsgId
            ? { ...m, isStreaming: false, sources, pipelineEvents: accumulatedEvents, degradationTrace }
            : m,
        ),
        currentSources: sources,
        isStreaming: false,
      }));
    } catch (err: unknown) {
      if (controller.signal.aborted || !isCurrent()) return;
      const errorMsg = err instanceof Error ? err.message : 'An unexpected error occurred';
      set((state) => ({
        messages: state.messages.map((m) =>
          m.id === assistantMsgId
            ? {
                ...m,
                content: accumulatedResponse || '⚠️ An error occurred while processing your request.',
                isStreaming: false,
              }
            : m,
        ),
        currentSteps: state.currentSteps.map((s) =>
          s.status === 'active' ? { ...s, status: 'error' as const } : s,
        ),
        isStreaming: false,
        error: errorMsg,
      }));
    } finally {
      if (activeStreamController === controller) {
        activeStreamController = null;
      }
    }
  },
}));
