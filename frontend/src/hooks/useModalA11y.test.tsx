import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { useModalA11y } from './useModalA11y';

function TestDialog({ isOpen, onClose }: { isOpen: boolean; onClose: () => void }) {
  const dialogRef = useModalA11y(isOpen, onClose);
  if (!isOpen) return null;
  return (
    <div>
      <button data-testid="opener">Opener</button>
      <div ref={dialogRef} role="dialog" aria-modal="true" tabIndex={-1}>
        <button>First</button>
        <button>Last</button>
      </div>
    </div>
  );
}

describe('useModalA11y', () => {
  it('calls onClose when Escape is pressed', () => {
    const onClose = vi.fn();
    render(<TestDialog isOpen onClose={onClose} />);

    fireEvent.keyDown(document, { key: 'Escape' });

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('does not attach the Escape listener when closed', () => {
    const onClose = vi.fn();
    render(<TestDialog isOpen={false} onClose={onClose} />);

    fireEvent.keyDown(document, { key: 'Escape' });

    expect(onClose).not.toHaveBeenCalled();
  });

  it('focuses the first focusable element inside the dialog on open', async () => {
    render(<TestDialog isOpen onClose={vi.fn()} />);

    await waitFor(() => expect(screen.getByText('First')).toHaveFocus());
  });

  it('wraps focus from the last element back to the first on Tab', () => {
    render(<TestDialog isOpen onClose={vi.fn()} />);

    const last = screen.getByText('Last');
    last.focus();
    fireEvent.keyDown(document, { key: 'Tab' });

    expect(screen.getByText('First')).toHaveFocus();
  });

  it('wraps focus from the first element back to the last on Shift+Tab', () => {
    render(<TestDialog isOpen onClose={vi.fn()} />);

    const first = screen.getByText('First');
    first.focus();
    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true });

    expect(screen.getByText('Last')).toHaveFocus();
  });
});
