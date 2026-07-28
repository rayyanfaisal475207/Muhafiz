// ============================================================
// Entity-resolution review queue (Phase 4.11)
//
// Every pending SAME_AS candidate the graph's entity-resolution pipeline
// flagged — flagged_unverified or human_review tier, never cnic_auto
// (those merge automatically and never need a human decision). Shows the
// BASIS for each match ("matched on name + shared case, unverified"),
// not a bare confidence percentage — that's what lets an investigator
// actually verify a match instead of rubber-stamping it.
// ============================================================

import React, { useCallback, useEffect, useState } from 'react'
import api from '../api'
import { Card, StatCard, formatWhen } from '../components/common'

interface EntitySummary {
  entity_id: string
  type: string
  canonical_name: string | null
  cnic: string | null
  plate: string | null
}

interface PendingMatch {
  edge_id: number
  tier: 'flagged_unverified' | 'human_review'
  confidence: number
  basis: string
  as_of: string | null
  source_doc_id: string | null
  source_chunk_id: string | null
  mention: EntitySummary
  candidate: EntitySummary
}

const TIER_LABEL: Record<string, string> = {
  flagged_unverified: 'Flagged — likely match',
  human_review: 'Human review — weak match',
}

const ReviewQueuePage: React.FC = () => {
  const [pending, setPending] = useState<PendingMatch[]>([])
  const [stats, setStats] = useState<Record<string, Record<string, number>>>({})
  const [tierFilter, setTierFilter] = useState<string>('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [acting, setActing] = useState<number | null>(null)

  const refresh = useCallback(async () => {
    setError(null)
    try {
      const [p, s] = await Promise.all([
        api.get<{ pending: PendingMatch[]; count: number }>('/graph-review/pending', {
          params: tierFilter ? { tier: tierFilter } : undefined,
        }),
        api.get<Record<string, Record<string, number>>>('/graph-review/stats'),
      ])
      setPending(p.data.pending)
      setStats(s.data)
    } catch (err: unknown) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        'Could not load the review queue.'
      setError(detail)
    } finally {
      setLoading(false)
    }
  }, [tierFilter])

  useEffect(() => { refresh() }, [refresh])

  const act = useCallback(async (edgeId: number, action: 'confirm' | 'reject') => {
    if (!window.confirm(`${action === 'confirm' ? 'Confirm' : 'Reject'} this entity match? This cannot be undone.`)) return
    setActing(edgeId)
    try {
      await api.post(`/graph-review/${edgeId}/${action}`, {})
      await refresh()
    } catch (err: unknown) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        `Could not ${action} this match.`
      setError(detail)
    } finally {
      setActing(null)
    }
  }, [refresh])

  const flaggedCount = (stats.flagged_unverified?.pending ?? 0)
  const reviewCount = (stats.human_review?.pending ?? 0)
  const confirmedCount = Object.values(stats).reduce((sum, s) => sum + (s.confirmed ?? 0), 0)
  const rejectedCount = Object.values(stats).reduce((sum, s) => sum + (s.rejected ?? 0), 0)

  return (
    <div className="main-content">
      <div className="page-header">
        <h1 className="page-title">Entity resolution review queue</h1>
        <p className="page-sub">
          Candidate matches the resolution pipeline found but never auto-merged — CNIC-exact
          matches merge automatically and never appear here. Confirm or reject with the basis
          shown; nothing here is a final fact until an investigator says so.
        </p>
      </div>

      <div className="page-body">
        {error && (
          <div className="login-error" style={{ marginBottom: 0 }}>{error}</div>
        )}

        <div className="stat-grid">
          <StatCard label="Flagged — likely match" value={flaggedCount} hint="pending review" />
          <StatCard label="Human review — weak match" value={reviewCount} hint="pending review" />
          <StatCard label="Confirmed" value={confirmedCount} hint="investigator-verified" tone="good" />
          <StatCard label="Rejected" value={rejectedCount} hint="investigator-rejected" tone="bad" />
        </div>

        <Card
          title="Pending matches"
          sub={`${pending.length} awaiting review`}
          right={
            <div className="segmented" role="group" aria-label="Tier filter">
              {['', 'flagged_unverified', 'human_review'].map((t) => (
                <button
                  key={t || 'all'}
                  className={tierFilter === t ? 'active' : ''}
                  onClick={() => setTierFilter(t)}
                >
                  {t ? TIER_LABEL[t] : 'All'}
                </button>
              ))}
            </div>
          }
        >
          {loading ? (
            <div className="loading-state">Loading…</div>
          ) : pending.length === 0 ? (
            <div className="empty-state">Nothing pending review right now.</div>
          ) : (
            <div className="overflow-x-auto">
              <table>
                <thead>
                  <tr>
                    <th>Tier</th>
                    <th>Mention</th>
                    <th>Candidate match</th>
                    <th>Basis</th>
                    <th className="num">Confidence</th>
                    <th>Flagged</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {pending.map((m) => (
                    <tr key={m.edge_id}>
                      <td>
                        <span className={`badge ${m.tier === 'flagged_unverified' ? 'good' : ''}`}>
                          {TIER_LABEL[m.tier] ?? m.tier}
                        </span>
                      </td>
                      <td style={{ color: 'var(--text-primary)', fontWeight: 500 }}>
                        {m.mention.canonical_name}
                        <div className="card-sub">{m.mention.type} · {m.mention.entity_id}</div>
                      </td>
                      <td style={{ color: 'var(--text-primary)', fontWeight: 500 }}>
                        {m.candidate.canonical_name}
                        <div className="card-sub">{m.candidate.type} · {m.candidate.entity_id}</div>
                      </td>
                      <td className="truncate" style={{ maxWidth: 320 }} title={m.basis}>
                        {m.basis}
                      </td>
                      <td className="num">{(m.confidence * 100).toFixed(0)}%</td>
                      <td style={{ whiteSpace: 'nowrap' }}>{formatWhen(m.as_of)}</td>
                      <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                        <button
                          className="btn btn-primary"
                          disabled={acting === m.edge_id}
                          onClick={() => act(m.edge_id, 'confirm')}
                          style={{ marginRight: 8 }}
                        >
                          {acting === m.edge_id ? '…' : 'Confirm'}
                        </button>
                        <button
                          className="btn btn-danger"
                          disabled={acting === m.edge_id}
                          onClick={() => act(m.edge_id, 'reject')}
                        >
                          Reject
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>
    </div>
  )
}

export default ReviewQueuePage
