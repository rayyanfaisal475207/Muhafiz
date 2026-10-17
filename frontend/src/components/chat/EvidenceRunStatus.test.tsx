import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { EvidenceRunStatus } from './EvidenceRunStatus';
import { useChatStore } from '../../store/chatStore';
import * as api from '../../lib/api';

vi.mock('../../lib/api', async () => ({
  ...await vi.importActual<typeof api>('../../lib/api'),
  cancelEvidenceRun: vi.fn(async () => undefined),
  getEvidenceRun: vi.fn(),
}));

describe('foreground evidence cancellation', () => {
  beforeEach(() => {
    useChatStore.getState().reset();
    vi.mocked(api.cancelEvidenceRun).mockClear();
    vi.mocked(api.getEvidenceRun).mockClear();
  });
  it('cancels the durable request during streaming without racing the SSE answer', async () => {
    useChatStore.setState({ messages: [{ id: 'message', role: 'assistant', content: '', isStreaming: true }] });
    render(<EvidenceRunStatus runId="run" messageId="message" status="running" streaming />);
    expect(api.getEvidenceRun).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(api.cancelEvidenceRun).toHaveBeenCalledWith('run'));
    expect(useChatStore.getState().messages[0].answerStatus).toBe('cancelled');
    expect(useChatStore.getState().messages[0].content).toBe('');
  });
});
