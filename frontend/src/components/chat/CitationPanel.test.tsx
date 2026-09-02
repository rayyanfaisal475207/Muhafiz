import { describe, it, expect, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { CitationPanel } from './CitationPanel'
import type { Source } from '../../types'

// Module 2 of FRONTEND_UX_MATURITY_IMPLEMENTATION_PLAN.md: CitationPanel
// went from a permanently-reserved layout column to an on-demand overlay.
// These guard the overlay's a11y contract (useModalA11y) and that it
// actually opens/closes cleanly rather than just typechecking.

const docSource: Source = { filename: 'FIR-1001-26.pdf', type: 'document', snippet: 'The complainant stated...' } as Source
const webSource: Source = { filename: 'https://example.com/news', type: 'web', snippet: 'A news excerpt.' } as Source

/** Mirrors how ChatPage actually wires this: a citation chip that mounts
 * CitationPanel on click and unmounts it on close, so useModalA11y's own
 * open/focus-return behavior is exercised through the real component. */
function Harness({ source }: { source: Source }) {
  const [open, setOpen] = useState(false)
  return (
    <div>
      <button onClick={() => setOpen(true)}>Open citation chip</button>
      {open && <CitationPanel source={source} onClose={() => setOpen(false)} />}
    </div>
  )
}

describe('CitationPanel — slide-in overlay', () => {
  it('renders as a dialog with the document title, not a static column', () => {
    render(<CitationPanel source={docSource} onClose={vi.fn()} />)
    const dialog = screen.getByRole('dialog')
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    expect(screen.getByText('Document Citation', { exact: false })).toBeInTheDocument()
    expect(screen.getByText('FIR-1001-26.pdf')).toBeInTheDocument()
  })

  it('shows the web-source treatment and an outbound link for a web citation', () => {
    render(<CitationPanel source={webSource} onClose={vi.fn()} />)
    expect(screen.getByText('Web Source', { exact: false })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Open Original URL/i })).toHaveAttribute(
      'href',
      'https://example.com/news',
    )
  })

  it('calls onClose when the close button is clicked', async () => {
    const onClose = vi.fn()
    render(<CitationPanel source={docSource} onClose={onClose} />)
    await userEvent.click(screen.getByLabelText('Close'))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('calls onClose when Escape is pressed', async () => {
    const onClose = vi.fn()
    render(<CitationPanel source={docSource} onClose={onClose} />)
    await userEvent.keyboard('{Escape}')
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('calls onClose when the backdrop (not the panel itself) is clicked', async () => {
    const onClose = vi.fn()
    const { container } = render(<CitationPanel source={docSource} onClose={onClose} />)
    const backdrop = container.firstElementChild as HTMLElement
    await userEvent.click(backdrop)
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('does not close when clicking inside the panel content', async () => {
    const onClose = vi.fn()
    render(<CitationPanel source={docSource} onClose={onClose} />)
    await userEvent.click(screen.getByText('FIR-1001-26.pdf'))
    expect(onClose).not.toHaveBeenCalled()
  })

  it('opens on click, closes on Escape, and returns focus to the chip that opened it', async () => {
    render(<Harness source={docSource} />)
    const opener = screen.getByText('Open citation chip')

    await userEvent.click(opener)
    expect(screen.getByRole('dialog')).toBeInTheDocument()

    await userEvent.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    await waitFor(() => expect(opener).toHaveFocus())
  })
})
