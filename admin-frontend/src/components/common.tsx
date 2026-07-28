// ============================================================
// Shared dashboard building blocks: stat cards, filters, banners.
// ============================================================

import React from 'react'

export function StatCard({
  label, value, hint, tone = 'neutral',
}: {
  label: string
  value: string | number
  hint?: string
  tone?: 'neutral' | 'good' | 'bad'
}) {
  return (
    <div className="stat-card">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {hint && <div className={`stat-hint ${tone === 'neutral' ? '' : tone}`}>{hint}</div>}
    </div>
  )
}

export function Card({
  title, sub, right, children,
}: {
  title: string
  sub?: string
  right?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <div className="card">
      <div className="row-between" style={{ marginBottom: sub ? 0 : 14 }}>
        <div>
          <div className="card-title">{title}</div>
          {sub && <div className="card-sub">{sub}</div>}
        </div>
        {right}
      </div>
      {children}
    </div>
  )
}

export const RANGES = [
  { days: 1, label: '24h', granularity: 'hour' as const },
  { days: 7, label: '7d', granularity: 'day' as const },
  { days: 30, label: '30d', granularity: 'day' as const },
  { days: 90, label: '90d', granularity: 'day' as const },
]

export function RangePicker({
  value, onChange,
}: {
  value: number
  onChange: (days: number, granularity: 'day' | 'hour') => void
}) {
  return (
    <div className="segmented" role="group" aria-label="Time range">
      {RANGES.map((r) => (
        <button
          key={r.days}
          className={value === r.days ? 'active' : ''}
          aria-pressed={value === r.days}
          onClick={() => onChange(r.days, r.granularity)}
        >
          {r.label}
        </button>
      ))}
    </div>
  )
}

/**
 * Shown when migration 003 has not been applied: the tables that back error
 * history and ingestion status do not exist yet. An empty chart would read as
 * "a healthy, silent system", which is a different claim from "we are not
 * recording this yet" — so we say which it is.
 */
function listOf(names: string[]): string {
  if (names.length === 1) return names[0]
  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`
}

export function InstrumentationBanner({ missing }: { missing: string[] }) {
  if (missing.length === 0) return null
  const plural = missing.length > 1
  return (
    <div className="banner banner-warning">
      <span aria-hidden>⚠</span>
      <span>
        <strong>Instrumentation not applied.</strong>{' '}
        The <code>{listOf(missing)}</code> table{plural ? 's are' : ' is'} missing, so
        this data is not being recorded yet. Run{' '}
        <code>migrations/003_admin_dashboard_and_attachments.sql</code> — via{' '}
        <code>python scripts/apply_migration.py</code>. Everything
        else on this dashboard is live.
      </span>
    </div>
  )
}

export function StatusBadge({ status }: { status: string }) {
  const cls =
    status === 'success' ? 'badge-success'
    : status === 'failed' ? 'badge-failed'
    : status === 'processing' ? 'badge-processing'
    : status === 'critical' ? 'badge-critical'
    : status === 'error' ? 'badge-error'
    : status === 'warning' ? 'badge-warning'
    : 'badge-accent'
  return <span className={`badge ${cls}`}>{status}</span>
}

export function formatBytes(bytes?: number): string {
  if (!bytes) return '—'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

export function formatWhen(iso?: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleString(undefined, {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  })
}
