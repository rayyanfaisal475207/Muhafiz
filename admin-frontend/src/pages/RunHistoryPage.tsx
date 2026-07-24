import React, { useEffect, useState } from 'react'
import api from '../api'

interface PipelineRun {
  run_id: string
  original_query: string
  rewritten_query: string
  routed_to: string
  final_outcome: string
  retry_count: number
  total_duration_ms: number
  created_at: string
}

interface PipelineStep {
  step_id: number
  step_name: string
  step_order: number
  status: string
  duration_ms: number
  input_summary: any
  output_summary: any
  created_at: string
}

const ROUTE_BADGE: Record<string, string> = {
  RAG: 'badge-rag',
  SQL: 'badge-sql',
  WEB: 'badge-web',
  DIRECT: 'badge-direct',
}

const STATUS_CLASS_MAP: Record<string, string> = {
  done: 'success',
  error: 'failed',
  active: 'active',
  skipped: 'skipped',
  retry: 'retry',
}

function fmtDate(iso: string) {
  return new Date(iso).toLocaleString('en-PK', { hour12: true, dateStyle: 'short', timeStyle: 'short' })
}

const RunRow: React.FC<{ run: PipelineRun }> = ({ run }) => {
  const [expanded, setExpanded] = useState(false)
  const [steps, setSteps] = useState<PipelineStep[] | null>(null)
  const [loadingSteps, setLoadingSteps] = useState(false)

  const toggleExpand = async () => {
    if (!expanded && steps === null) {
      setLoadingSteps(true)
      try {
        const res = await api.get(`/runs/${run.run_id}/steps`)
        setSteps(res.data)
      } catch {
        setSteps([])
      } finally {
        setLoadingSteps(false)
      }
    }
    setExpanded(v => !v)
  }

  const badgeClass = ROUTE_BADGE[run.routed_to?.toUpperCase()] || 'badge-unknown'

  return (
    <>
      <tr onClick={toggleExpand} style={{ cursor: 'pointer' }}>
        <td>
          <span style={{ fontSize: 11, color: 'var(--gold)', marginRight: 6 }}>
            {expanded ? '▼' : '▶'}
          </span>
          <span className="td-mono">{run.run_id.slice(0, 8)}…</span>
        </td>
        <td style={{ maxWidth: 300, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {run.original_query || '—'}
        </td>
        <td>
          <span className={`badge ${badgeClass}`}>{run.routed_to || 'unknown'}</span>
        </td>
        <td>{run.total_duration_ms ? `${run.total_duration_ms}ms` : '—'}</td>
        <td>{run.retry_count}</td>
        <td style={{ color: 'var(--text-muted)', fontSize: 12 }}>
          {run.created_at ? fmtDate(run.created_at) : '—'}
        </td>
      </tr>
      {expanded && (
        <tr className="expand-row">
          <td colSpan={6}>
            <div className="expand-panel">
              {loadingSteps && <div style={{ color: 'var(--text-muted)', fontSize: 12 }}>Loading steps…</div>}
              {steps !== null && steps.length === 0 && (
                <div style={{ color: 'var(--text-muted)', fontSize: 12 }}>No steps recorded for this run.</div>
              )}
              {steps && steps.length > 0 && (
                <div className="step-list">
                  {steps.map(s => (
                    <div className="step-item" key={s.step_id}>
                      <div className={`step-dot step-dot-${STATUS_CLASS_MAP[s.status] || s.status}`} />
                      <span className="step-name">{s.step_name}</span>
                      <span style={{ color: 'var(--text-secondary)', fontSize: 12 }}>
                        {s.status}
                      </span>
                      <span className="step-ms">
                        {s.duration_ms != null ? `${s.duration_ms}ms` : '—'}
                      </span>
                    </div>
                  ))}
                </div>
              )}
              <div style={{ marginTop: 12, fontSize: 11.5, color: 'var(--text-muted)' }}>
                <strong style={{ color: 'var(--text-secondary)' }}>Rewritten:</strong>{' '}
                {run.rewritten_query || '—'}
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  )
}

const RunHistoryPage: React.FC = () => {
  const [runs, setRuns] = useState<PipelineRun[]>([])
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState<string | null>(null)
  const [error, setError] = useState('')

  const fetchRuns = async (route?: string) => {
    setLoading(true)
    setError('')
    try {
      const params: any = { limit: 100 }
      if (route) params.route_filter = route
      const res = await api.get('/runs', { params })
      setRuns(res.data)
    } catch (e: any) {
      setError('Failed to load runs')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { fetchRuns(filter || undefined) }, [filter])

  return (
    <div className="main-content">
      <div className="page-header">
        <div>
          <div className="page-title">Run History</div>
          <div className="page-subtitle">All pipeline runs — click any row to expand step trace</div>
        </div>
      </div>

      <div className="page-body">
        <div className="table-wrapper">
          <div className="table-header-bar">
            <div className="filter-bar">
              {[null, 'RAG', 'SQL', 'WEB', 'DIRECT'].map(r => (
                <button
                  key={String(r)}
                  className={`filter-btn${filter === r ? ' active' : ''}`}
                  onClick={() => setFilter(r)}
                >
                  {r ?? 'All'}
                </button>
              ))}
            </div>
            <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>
              {runs.length} run{runs.length !== 1 ? 's' : ''}
            </span>
          </div>

          {loading && <div className="loading-state"><div className="spinner"/><span>Loading…</span></div>}
          {error && <div className="loading-state" style={{ color: 'var(--error)' }}>{error}</div>}

          {!loading && !error && (
            runs.length === 0 ? (
              <div className="empty-state">No pipeline runs found.</div>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th>Run ID</th>
                    <th>Query</th>
                    <th>Route</th>
                    <th>Duration</th>
                    <th>Retries</th>
                    <th>Time</th>
                  </tr>
                </thead>
                <tbody>
                  {runs.map(run => <RunRow key={run.run_id} run={run} />)}
                </tbody>
              </table>
            )
          )}
        </div>
      </div>
    </div>
  )
}

export default RunHistoryPage
