// ============================================================
// Ingestion quality — Ingestion Quality Control at Scale, Module G4
// (INGESTION_QUALITY_AT_SCALE_PLAN.md). Read-only surface for G1's
// per-run entity-resolution tier rollup and G2's circuit-breaker flags.
//
// PLACEMENT DECISION (resolved during implementation, per the plan's own
// deferred point): a NEW page, not an extension of KnowledgeBasePage.
// KnowledgeBasePage's "Ingestion status" table is per-DOCUMENT
// (ingestion_jobs — the single-file admin-upload path only). This page
// is per-RUN (ingestion_run_quality — both ingest_file's one-row-per-
// document runs AND sync_muhafiz_data's whole-corpus bulk-sync runs,
// which have no corresponding "document" row in KB's jobs list at all)
// and shows a genuinely different thing: entity-resolution tier outcomes
// and a flag/acknowledge action, not upload/chunking status. Folding
// both into one page would conflate two different operational
// questions ("did my upload work?" vs "is entity resolution holding up
// at this volume?"). Same access tier as Review Queue (`supervisor` or
// higher) — this is the same "is the graph pipeline behaving" concern.
//
// No new access-control surface: the backend endpoints already require
// `supervisor` (src/api/ingestion_quality_admin.py), matching this
// page's route guard in App.tsx.
// ============================================================

import React, { useCallback, useEffect, useState } from 'react'
import api from '../api'
import { Card, StatCard, formatWhen } from '../components/common'

interface Run {
  run_id: string
  source: string
  case_id: string | null
  started_at: string | null
  finished_at: string | null
  tier_cnic_auto: number
  tier_flagged_unverified: number
  tier_human_review: number
  tier_new: number
  corroboration_gate_rejections: number
  extraction_errors: number
  flagged_for_review: boolean
  flagged_reason: string | null
}

const SOURCE_LABEL: Record<string, string> = {
  ingest_file: 'Single-document upload',
  sync_muhafiz_data: 'Muhafiz Data API sync',
}

function totalResolved(r: Run): number {
  return r.tier_cnic_auto + r.tier_flagged_unverified + r.tier_human_review + r.tier_new
}

function ambiguousRate(r: Run): number | null {
  const total = totalResolved(r)
  if (total === 0) return null
  return (r.tier_flagged_unverified + r.tier_human_review) / total
}

const IngestionQualityPage: React.FC = () => {
  const [runs, setRuns] = useState<Run[]>([])
  const [sourceFilter, setSourceFilter] = useState<string>('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [acking, setAcking] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    setError(null)
    try {
      const r = await api.get<{ runs: Run[]; count: number }>('/ingestion-quality/runs', {
        params: { limit: 100, ...(sourceFilter ? { source: sourceFilter } : {}) },
      })
      setRuns(r.data.runs)
    } catch (err: unknown) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        'Could not load ingestion runs.'
      setError(detail)
    } finally {
      setLoading(false)
    }
  }, [sourceFilter])

  useEffect(() => { refresh() }, [refresh])

  const acknowledge = useCallback(async (runId: string) => {
    if (!window.confirm(`Acknowledge the flag on run "${runId}"? The next run for this source will stop inheriting it.`)) return
    setAcking(runId)
    try {
      await api.post(`/ingestion-quality/${encodeURIComponent(runId)}/acknowledge`, {})
      await refresh()
    } catch (err: unknown) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        'Could not acknowledge this run.'
      setError(detail)
    } finally {
      setAcking(null)
    }
  }, [refresh])

  const flaggedCount = runs.filter((r) => r.flagged_for_review).length
  const totalRuns = runs.length
  const totalMentions = runs.reduce((sum, r) => sum + totalResolved(r), 0)
  const totalErrors = runs.reduce((sum, r) => sum + r.extraction_errors, 0)

  return (
    <div className="main-content">
      <div className="page-header">
        <h1 className="page-title">Ingestion quality</h1>
        <p className="page-sub">
          Per-run entity-resolution outcomes (Module G1) and the circuit breaker's flags (Module G2) —
          every figure below is a real count from <code>ingestion_run_quality</code>, nothing estimated.
        </p>
      </div>

      <div className="page-body">
        {error && (
          <div className="login-error" style={{ marginBottom: 0 }}>{error}</div>
        )}

        <div className="stat-grid">
          <StatCard label="Runs shown" value={totalRuns} hint="most recent 100" />
          <StatCard label="Mentions resolved" value={totalMentions} hint="across shown runs" />
          <StatCard
            label="Flagged runs"
            value={flaggedCount}
            hint={flaggedCount > 0 ? 'awaiting acknowledgment' : 'none'}
            tone={flaggedCount > 0 ? 'bad' : 'good'}
          />
          <StatCard
            label="Extraction errors"
            value={totalErrors}
            hint={totalErrors > 0 ? 'see the table below' : 'none'}
            tone={totalErrors > 0 ? 'bad' : 'good'}
          />
        </div>

        <Card
          title="Ingestion runs"
          sub={`${runs.length} shown, newest first`}
          right={
            <div className="segmented" role="group" aria-label="Source filter">
              {['', 'ingest_file', 'sync_muhafiz_data'].map((s) => (
                <button
                  key={s || 'all'}
                  className={sourceFilter === s ? 'active' : ''}
                  aria-pressed={sourceFilter === s}
                  onClick={() => setSourceFilter(s)}
                >
                  {s ? SOURCE_LABEL[s] ?? s : 'All'}
                </button>
              ))}
            </div>
          }
        >
          {loading ? (
            <div className="loading-state">Loading…</div>
          ) : runs.length === 0 ? (
            <div className="empty-state">No ingestion runs recorded yet.</div>
          ) : (
            <div className="overflow-x-auto">
              <table>
                <thead>
                  <tr>
                    <th>Run</th>
                    <th>Source</th>
                    <th>Case</th>
                    <th className="num">CNIC-auto</th>
                    <th className="num">Flagged</th>
                    <th className="num">Human review</th>
                    <th className="num">New</th>
                    <th className="num">Gate rejections</th>
                    <th className="num">Ambiguous rate</th>
                    <th className="num">Errors</th>
                    <th>Started</th>
                    <th>Status</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {runs.map((r) => {
                    const rate = ambiguousRate(r)
                    return (
                      <tr key={r.run_id}>
                        <td className="truncate" style={{ maxWidth: 220, color: 'var(--text-primary)', fontWeight: 500 }} title={r.run_id}>
                          {r.run_id}
                        </td>
                        <td><span className="badge">{SOURCE_LABEL[r.source] ?? r.source}</span></td>
                        <td className="truncate" style={{ maxWidth: 140 }}>{r.case_id ?? '—'}</td>
                        <td className="num">{r.tier_cnic_auto}</td>
                        <td className="num">{r.tier_flagged_unverified}</td>
                        <td className="num">{r.tier_human_review}</td>
                        <td className="num">{r.tier_new}</td>
                        <td className="num">{r.corroboration_gate_rejections}</td>
                        <td className="num">{rate === null ? '—' : `${(rate * 100).toFixed(0)}%`}</td>
                        <td className="num" style={{ color: r.extraction_errors > 0 ? 'var(--error)' : undefined }}>
                          {r.extraction_errors}
                        </td>
                        <td style={{ whiteSpace: 'nowrap' }}>{formatWhen(r.started_at)}</td>
                        <td>
                          {r.finished_at === null ? (
                            <span className="badge badge-processing">in progress</span>
                          ) : r.flagged_for_review ? (
                            <span className="badge badge-warning" title={r.flagged_reason ?? undefined}>flagged</span>
                          ) : (
                            <span className="badge badge-success">ok</span>
                          )}
                        </td>
                        <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                          {r.flagged_for_review && (
                            <button
                              className="btn btn-primary"
                              disabled={acking === r.run_id}
                              onClick={() => acknowledge(r.run_id)}
                            >
                              {acking === r.run_id ? '…' : 'Acknowledge'}
                            </button>
                          )}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>
    </div>
  )
}

export default IngestionQualityPage
