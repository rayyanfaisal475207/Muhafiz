import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { CaseSelector } from './CaseSelector'
import type { Case } from '../../store/caseStore'

function makeCase(case_id: string, fir_number?: string): Case {
  return {
    case_id,
    fir_number,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  } as Case
}

const CASES: Case[] = [
  makeCase('fir-891-24', '891/24'),
  makeCase('fir-430-26', '430/26'),
  makeCase('fir-416-26', '416/26'),
]

describe('CaseSelector — closed state', () => {
  it('shows the allCasesLabel when no case is active', () => {
    render(<CaseSelector cases={CASES} activeCaseId={null} allCasesLabel="All Cases" onSelect={vi.fn()} />)
    expect(screen.getByRole('combobox', { name: 'Case' })).toHaveValue('All Cases')
  })

  it('shows the active case\'s FIR number when one is selected', () => {
    render(<CaseSelector cases={CASES} activeCaseId="fir-430-26" allCasesLabel="All Cases" onSelect={vi.fn()} />)
    expect(screen.getByRole('combobox', { name: 'Case' })).toHaveValue('430/26')
  })

  it('falls back to case_id when a case has no fir_number', () => {
    const cases = [makeCase('legacy-case-1')]
    render(<CaseSelector cases={cases} activeCaseId="legacy-case-1" allCasesLabel="All Cases" onSelect={vi.fn()} />)
    expect(screen.getByRole('combobox', { name: 'Case' })).toHaveValue('legacy-case-1')
  })
})

describe('CaseSelector — search and selection', () => {
  it('opens the list and shows every case plus All Cases on focus', () => {
    render(<CaseSelector cases={CASES} activeCaseId={null} allCasesLabel="All Cases" onSelect={vi.fn()} />)
    fireEvent.focus(screen.getByRole('combobox', { name: 'Case' }))

    const listbox = screen.getByRole('listbox')
    expect(listbox).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'All Cases' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: '891/24' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: '430/26' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: '416/26' })).toBeInTheDocument()
  })

  it('filters the list as the user types, matching the FIR number', () => {
    render(<CaseSelector cases={CASES} activeCaseId={null} allCasesLabel="All Cases" onSelect={vi.fn()} />)
    const input = screen.getByRole('combobox', { name: 'Case' })
    fireEvent.focus(input)
    fireEvent.change(input, { target: { value: '430' } })

    expect(screen.getByRole('option', { name: '430/26' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: '891/24' })).not.toBeInTheDocument()
    expect(screen.queryByRole('option', { name: '416/26' })).not.toBeInTheDocument()
  })

  it('filters by the underlying case_id too, not just the displayed FIR number', () => {
    render(<CaseSelector cases={CASES} activeCaseId={null} allCasesLabel="All Cases" onSelect={vi.fn()} />)
    const input = screen.getByRole('combobox', { name: 'Case' })
    fireEvent.focus(input)
    fireEvent.change(input, { target: { value: 'fir-416' } })

    expect(screen.getByRole('option', { name: '416/26' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: '891/24' })).not.toBeInTheDocument()
  })

  it('shows a "no match" message when the search matches nothing', () => {
    render(<CaseSelector cases={CASES} activeCaseId={null} allCasesLabel="All Cases" onSelect={vi.fn()} />)
    const input = screen.getByRole('combobox', { name: 'Case' })
    fireEvent.focus(input)
    fireEvent.change(input, { target: { value: 'zzz-nonexistent' } })

    expect(screen.getByText(/No cases match/)).toBeInTheDocument()
  })

  it('calls onSelect with the case_id when a case is clicked, and closes the list', () => {
    const onSelect = vi.fn()
    render(<CaseSelector cases={CASES} activeCaseId={null} allCasesLabel="All Cases" onSelect={onSelect} />)
    const input = screen.getByRole('combobox', { name: 'Case' })
    fireEvent.focus(input)
    fireEvent.mouseDown(screen.getByRole('option', { name: '430/26' }))

    expect(onSelect).toHaveBeenCalledWith('fir-430-26')
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
  })

  it('calls onSelect with null when "All Cases" is clicked', () => {
    const onSelect = vi.fn()
    render(<CaseSelector cases={CASES} activeCaseId="fir-430-26" allCasesLabel="All Cases" onSelect={onSelect} />)
    const input = screen.getByRole('combobox', { name: 'Case' })
    fireEvent.focus(input)
    fireEvent.mouseDown(screen.getByRole('option', { name: 'All Cases' }))

    expect(onSelect).toHaveBeenCalledWith(null)
  })

  it('selects the highlighted row on Enter', () => {
    const onSelect = vi.fn()
    render(<CaseSelector cases={CASES} activeCaseId={null} allCasesLabel="All Cases" onSelect={onSelect} />)
    const input = screen.getByRole('combobox', { name: 'Case' })
    fireEvent.focus(input)
    fireEvent.change(input, { target: { value: '430' } })
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    fireEvent.keyDown(input, { key: 'Enter' })

    expect(onSelect).toHaveBeenCalledWith('fir-430-26')
  })

  it('closes without selecting on Escape', () => {
    const onSelect = vi.fn()
    render(<CaseSelector cases={CASES} activeCaseId={null} allCasesLabel="All Cases" onSelect={onSelect} />)
    const input = screen.getByRole('combobox', { name: 'Case' })
    fireEvent.focus(input)
    fireEvent.keyDown(input, { key: 'Escape' })

    expect(onSelect).not.toHaveBeenCalled()
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
  })

  it('closes when clicking outside', () => {
    render(
      <div>
        <CaseSelector cases={CASES} activeCaseId={null} allCasesLabel="All Cases" onSelect={vi.fn()} />
        <button>outside</button>
      </div>,
    )
    fireEvent.focus(screen.getByRole('combobox', { name: 'Case' }))
    expect(screen.getByRole('listbox')).toBeInTheDocument()

    fireEvent.mouseDown(screen.getByText('outside'))
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
  })
})
