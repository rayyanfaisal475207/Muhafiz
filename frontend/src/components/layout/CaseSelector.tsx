// ============================================================
// CaseSelector — searchable case-switcher, replacing a plain <select>.
//
// A native <select> doesn't scale: once there are dozens/hundreds of
// cases, scrolling a long unfiltered dropdown to find one FIR number is
// slow and easy to mis-click. This is a type-to-filter combobox instead —
// same underlying selection (case_id or null for "All Cases"/"No Case"),
// just findable by typing any part of the FIR number/case_id instead of
// scanning a long list.
// ============================================================

import { useEffect, useMemo, useRef, useState } from 'react';
import type { Case } from '../../store/caseStore';

interface Props {
  cases: Case[];
  activeCaseId: string | null;
  allCasesLabel: string;
  onSelect: (caseId: string | null) => void;
}

function caseLabel(c: Case): string {
  return c.fir_number || c.case_id;
}

export function CaseSelector({ cases, activeCaseId, allCasesLabel, onSelect }: Props) {
  const [isOpen, setIsOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [highlightedIndex, setHighlightedIndex] = useState(0);
  const containerRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const activeCase = activeCaseId ? cases.find((c) => c.case_id === activeCaseId) : null;
  const closedDisplayValue = activeCase ? caseLabel(activeCase) : allCasesLabel;

  const filteredCases = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return cases;
    return cases.filter(
      (c) => caseLabel(c).toLowerCase().includes(q) || c.case_id.toLowerCase().includes(q),
    );
  }, [cases, query]);

  // The "All Cases"/"No Case" option stays in the list unless the search
  // text clearly isn't trying to match it — so it's always reachable by
  // clearing the search, never hidden by a query aimed at a specific case.
  const showAllCasesOption =
    !query.trim() || allCasesLabel.toLowerCase().includes(query.trim().toLowerCase());

  // Flat list of selectable rows this render, in display order — keyboard
  // navigation and click-selection both index into this same array so
  // they can never disagree about what row N is.
  const rows: Array<{ caseId: string | null; label: string }> = [
    ...(showAllCasesOption ? [{ caseId: null, label: allCasesLabel }] : []),
    ...filteredCases.map((c) => ({ caseId: c.case_id, label: caseLabel(c) })),
  ];

  useEffect(() => {
    setHighlightedIndex(0);
  }, [query, isOpen]);

  useEffect(() => {
    if (!isOpen) return;
    function handleClickOutside(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        close();
      }
    }
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isOpen]);

  function open() {
    setQuery('');
    setIsOpen(true);
  }

  function close() {
    setIsOpen(false);
    setQuery('');
  }

  function select(caseId: string | null) {
    onSelect(caseId);
    close();
    inputRef.current?.blur();
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (!isOpen) {
      if (e.key === 'ArrowDown' || e.key === 'Enter') {
        e.preventDefault();
        open();
      }
      return;
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setHighlightedIndex((i) => Math.min(i + 1, rows.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setHighlightedIndex((i) => Math.max(i - 1, 0));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      const row = rows[highlightedIndex];
      if (row) select(row.caseId);
    } else if (e.key === 'Escape') {
      e.preventDefault();
      close();
      inputRef.current?.blur();
    }
  }

  return (
    <div ref={containerRef} className="relative">
      <input
        ref={inputRef}
        role="combobox"
        aria-expanded={isOpen}
        aria-controls="case-selector-listbox"
        aria-label="Case"
        aria-autocomplete="list"
        autoComplete="off"
        className="w-full bg-[var(--bg-surface)] border border-[var(--border)] rounded-sm px-2.5 py-1.5 text-sm text-[var(--text-primary)] transition-colors hover:border-[var(--border-hover)] focus:outline-none focus:border-[var(--accent)]"
        value={isOpen ? query : closedDisplayValue}
        placeholder={isOpen ? 'Search cases…' : undefined}
        onFocus={open}
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={handleKeyDown}
      />
      {isOpen && (
        <ul
          id="case-selector-listbox"
          role="listbox"
          aria-label="Cases"
          className="absolute z-20 mt-1 w-full max-h-64 overflow-y-auto rounded-sm border shadow-lg"
          style={{ background: 'var(--bg-surface)', borderColor: 'var(--border)' }}
        >
          {rows.length === 0 ? (
            <li className="px-2.5 py-2 text-sm" style={{ color: 'var(--text-faint)' }}>
              No cases match “{query}”.
            </li>
          ) : (
            rows.map((row, i) => (
              <li
                key={row.caseId ?? '__all_cases__'}
                role="option"
                aria-selected={row.caseId === activeCaseId}
                onMouseDown={(e) => {
                  // mousedown (not click) so this fires before the input's
                  // own onBlur/click-outside handling would otherwise close
                  // the list first and swallow the selection.
                  e.preventDefault();
                  select(row.caseId);
                }}
                onMouseEnter={() => setHighlightedIndex(i)}
                className="px-2.5 py-1.5 text-sm cursor-pointer truncate"
                style={{
                  background: i === highlightedIndex ? 'var(--accent-soft)' : undefined,
                  color: row.caseId === activeCaseId ? 'var(--accent)' : 'var(--text-primary)',
                  fontWeight: row.caseId === activeCaseId ? 600 : 400,
                }}
              >
                {row.label}
              </li>
            ))
          )}
        </ul>
      )}
    </div>
  );
}
