import { useState } from 'react';
import { useCaseStore } from '../../store/caseStore';

interface CaseSettingsModalProps {
  isOpen: boolean;
  onClose: () => void;
}

// Minimal create-a-case modal — a dropdown + create-modal is all Phase 1
// needs. The full Case Investigation Workspace (architecture doc's richer
// case view) is a later, separate UI, not this.
export function CaseSettingsModal({ isOpen, onClose }: CaseSettingsModalProps) {
  const { createCase } = useCaseStore();
  const [firNumber, setFirNumber] = useState('');
  const [crimeCategory, setCrimeCategory] = useState('');
  const [investigationOfficer, setInvestigationOfficer] = useState('');
  const [policeStation, setPoliceStation] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!isOpen) return null;

  const reset = () => {
    setFirNumber('');
    setCrimeCategory('');
    setInvestigationOfficer('');
    setPoliceStation('');
    setError(null);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setIsLoading(true);

    try {
      await createCase({
        fir_number: firNumber || undefined,
        crime_category: crimeCategory || undefined,
        investigation_officer: investigationOfficer || undefined,
        police_station: policeStation || undefined,
        investigation_status: 'open',
      });
      reset();
      onClose();
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to save case');
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <div className="rounded-lg w-full max-w-lg overflow-hidden" style={{ background: 'var(--bg-surface)', border: '1px solid var(--border)', boxShadow: 'var(--shadow-lg)' }}>
        <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--border)]" style={{ background: 'var(--bg-surface-2)' }}>
          <h2 className="text-xl font-bold text-[var(--text-primary)]">New Case</h2>
          <button onClick={() => { reset(); onClose(); }} className="p-1 rounded-xs text-[var(--text-muted)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-surface-3)] transition-colors">
            <svg className="w-6 h-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        <form onSubmit={handleSubmit} className="p-6">
          {error && (
            <div
              className="mb-4 p-3 rounded text-sm"
              style={{
                background: 'var(--error-soft)',
                border: '1px solid color-mix(in srgb, var(--error) 30%, transparent)',
                color: 'var(--error)',
              }}
            >
              {error}
            </div>
          )}

          <div className="space-y-4">
            <div>
              <label className="block text-[13px] font-medium text-[var(--text-secondary)] mb-1">
                FIR Number
              </label>
              <input
                type="text"
                className="w-full rounded-sm px-3 py-2 text-[15px] bg-[var(--bg-surface)] text-[var(--text-primary)] border border-[var(--border-strong)] transition-colors hover:border-[var(--border-hover)] focus:outline-none focus:border-[var(--accent)]"
                value={firNumber}
                onChange={(e) => setFirNumber(e.target.value)}
                placeholder="E.g., FIR-2026-THEFT-014"
              />
            </div>

            <div>
              <label className="block text-[13px] font-medium text-[var(--text-secondary)] mb-1">
                Crime Category
              </label>
              <input
                type="text"
                className="w-full rounded-sm px-3 py-2 text-[15px] bg-[var(--bg-surface)] text-[var(--text-primary)] border border-[var(--border-strong)] transition-colors hover:border-[var(--border-hover)] focus:outline-none focus:border-[var(--accent)]"
                value={crimeCategory}
                onChange={(e) => setCrimeCategory(e.target.value)}
                placeholder="E.g., Burglary"
              />
            </div>

            <div>
              <label className="block text-[13px] font-medium text-[var(--text-secondary)] mb-1">
                Investigation Officer
              </label>
              <input
                type="text"
                className="w-full rounded-sm px-3 py-2 text-[15px] bg-[var(--bg-surface)] text-[var(--text-primary)] border border-[var(--border-strong)] transition-colors hover:border-[var(--border-hover)] focus:outline-none focus:border-[var(--accent)]"
                value={investigationOfficer}
                onChange={(e) => setInvestigationOfficer(e.target.value)}
                placeholder="E.g., SI Ahmed Raza"
              />
            </div>

            <div>
              <label className="block text-[13px] font-medium text-[var(--text-secondary)] mb-1">
                Police Station
              </label>
              <input
                type="text"
                className="w-full rounded-sm px-3 py-2 text-[15px] bg-[var(--bg-surface)] text-[var(--text-primary)] border border-[var(--border-strong)] transition-colors hover:border-[var(--border-hover)] focus:outline-none focus:border-[var(--accent)]"
                value={policeStation}
                onChange={(e) => setPoliceStation(e.target.value)}
                placeholder="E.g., Margalla"
              />
            </div>
          </div>

          <div className="mt-8 flex justify-end gap-3">
            <button
              type="button"
              onClick={() => { reset(); onClose(); }}
              className="btn-ghost px-4 py-2 text-sm"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={isLoading}
              className="btn-accent px-4 py-2 text-sm"
            >
              {isLoading ? 'Saving...' : 'Create Case'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
