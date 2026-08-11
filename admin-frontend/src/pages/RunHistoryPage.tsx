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

/**
 * Structured per-query degradation trace, written by the harness supervisor
 * into pipeline_steps.output_summary.trace (one payload per sub-agent
 * completion, identical shape whichever sub-agent ran).
 *
 * Renders nothing for steps that carry no trace — legacy rows and per-tool
 * steps both fall through silently, so this is additive to the existing view.
 */
interface DegradationTrace {
  v: number
  sub_agent_status: string
  tools_used: string[]
  degraded_from: string[]
  contributed_only: string[]
  degraded_and_contributed: string[]
  degraded_only: string[]
  caveats: string[]
  disclosure_rendered: boolean | null
}

const TRACE_CHIP: React.CSSProperties = {
  display: 'inline-block', padding: '1px 6px', borderRadius: 4,
  fontSize: 11, marginRight: 4, marginBottom: 2,
}

export function StepTrace({ summary }: { summary: any }) {
  const trace: DegradationTrace | undefined = summary?.trace
  if (!trace) return null

  // A tool in BOTH lists contributed data but degraded internally while doing
  // so (e.g. RAG returning evidence with its relevance gate unavailable).
  // Rendering it as merely "used" would overstate the evidence base; rendering
  // it as merely "degraded" would understate what the answer rests on. It gets
  // its own label rather than being folded into either.
  const nothingToShow =
    trace.contributed_only.length === 0 &&
    trace.degraded_and_contributed.length === 0 &&
    trace.degraded_only.length === 0 &&
    trace.caveats.length === 0

  if (nothingToShow) return null

  return (
    <div style={{ margin: '2px 0 8px 22px', fontSize: 11.5, lineHeight: 1.7 }}>
      {trace.contributed_only.map(t => (
        <span key={t} style={{ ...TRACE_CHIP, background: 'var(--success-bg, #e6f4ea)', color: 'var(--success-fg, #1e7c3a)' }}>
          {t} · contributed
        </span>
      ))}
      {trace.degraded_and_contributed.map(t => (
        <span key={t} style={{ ...TRACE_CHIP, background: 'var(--warn-bg, #fdf3e0)', color: 'var(--warn-fg, #96620a)' }}>
          {t} · contributed, degraded
        </span>
      ))}
      {trace.degraded_only.map(t => (
        <span key={t} style={{ ...TRACE_CHIP, background: 'var(--muted-bg, #f0f0f0)', color: 'var(--text-muted)' }}>
          {t} · fell back
        </span>
      ))}
      {trace.disclosure_rendered === true && (
        <span style={{ ...TRACE_CHIP, background: 'var(--warn-bg, #fdf3e0)', color: 'var(--warn-fg, #96620a)' }}>
          disclosure in document
        </span>
      )}
      {trace.caveats.map((c, i) => (
        <div key={i} style={{ color: 'var(--text-muted)', marginTop: 2 }}>⚠ {c}</div>
      ))}
    </div>
  )
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

  const badgeClass = ROUTE_BADGE[run.routed_to?.toUpperCase()] || ''

  return (
    <>
      <tr onClick={toggleExpand} style={{ cursor: 'pointer' }}>
        <td>
          <span style={{ fontSize: 11, color: 'var(--accent)', marginRight: 6 }}>
            {expanded ? '▼' : '▶'}
          </span>
          <span className="font-mono">{run.run_id.slice(0, 8)}…</span>
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
                    <div key={s.step_id}>
                      <div className="step-item">
                        <div className={`step-dot step-dot-${STATUS_CLASS_MAP[s.status] || s.status}`} />
                        <span className="step-name">{s.step_name}</span>
                        <span style={{ color: 'var(--text-secondary)', fontSize: 12 }}>
                          {s.status}
                        </span>
                        <span className="step-ms">
                          {s.duration_ms != null ? `${s.duration_ms}ms` : '—'}
                        </span>
                      </div>
                      <StepTrace summary={s.output_summary} />
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
          <div className="page-sub">All pipeline runs — click any row to expand step trace</div>
        </div>
      </div>

      <div className="page-body">
        <div className="overflow-x-auto">
          <div className="table-header-bar">
            <div className="segmented" role="group" aria-label="Route filter">
              {[null, 'RAG', 'SQL', 'WEB', 'DIRECT'].map(r => (
                <button
                  key={String(r)}
                  className={filter === r ? 'active' : ''}
                  aria-pressed={filter === r}
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
