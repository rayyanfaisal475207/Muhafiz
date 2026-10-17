import { useEffect, useState } from 'react';
import { cancelEvidenceRun, downloadEvidenceArtifact, getEvidenceRun } from '../../lib/api';
import { useChatStore } from '../../store/chatStore';

interface Props {
  runId: string;
  messageId: string;
  status?: string;
  artifactUrl?: string;
  streaming?: boolean;
}

export function EvidenceRunStatus({ runId, messageId, status, artifactUrl, streaming = false }: Props) {
  const [error, setError] = useState('');
  const [discovery, setDiscovery] = useState<{ completed: number; total: number }>();
  const pending = status === 'pending' || status === 'running';
  useEffect(() => {
    if (!pending || streaming) return;
    let stopped = false;
    const poll = async () => {
      try {
        const run = await getEvidenceRun(runId);
        if (stopped) return;
        setError('');
        if (run.discovery) setDiscovery(run.discovery);
        if (!run.response || run.status === 'pending' || run.status === 'running') return;
        const result = run.response;
        useChatStore.setState((state) => ({
          messages: state.messages.map((m) => m.id === messageId ? {
            ...m, content: result.text, answerStatus: result.status as typeof m.answerStatus,
            artifactUrl: result.artifact_url ?? undefined,
            thinkingLogs: result.health?.trace?.map((entry) => entry.title) ?? m.thinkingLogs,
            sources: result.results.flatMap((r) => r.citations.map((s) => ({
              filename: s.title, span_id: s.span_id, doc_id: s.document_id,
              snippet: s.text, start: s.start, end: s.end,
            }))),
          } : m),
        }));
      } catch {
        if (!stopped) setError('Could not refresh this request. Reload the conversation to retry.');
      }
    };
    const timer = setInterval(poll, 5000);
    void poll();
    return () => { stopped = true; clearInterval(timer); };
  }, [pending, runId, messageId, streaming]);
  const cancel = async () => {
    try {
      await cancelEvidenceRun(runId);
      useChatStore.setState((state) => ({ messages: state.messages.map((m) => m.id === messageId
        ? { ...m, answerStatus: 'cancelled', content: streaming ? m.content : `${m.content}\n\nRequest cancelled.` } : m) }));
    } catch { setError('Could not cancel this request. Please retry.'); }
  };
  return <div className="mt-2 text-sm" role="status">
    {pending && <span>{streaming ? 'Checking the evidence' : 'Reviewing additional source documents'}{discovery?.total ? ` (${discovery.completed}/${discovery.total})` : ''}… <button className="underline" onClick={cancel}>Cancel</button></span>}
    {artifactUrl && <button className="underline" onClick={() => {
      void downloadEvidenceArtifact(artifactUrl).catch(() => setError('The report could not be downloaded.'));
    }}>Download report</button>}
    {error && <p>{error}</p>}
  </div>;
}
