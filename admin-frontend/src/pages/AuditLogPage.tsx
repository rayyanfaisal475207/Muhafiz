import React, { useCallback, useEffect, useState } from 'react'
import api from '../api'
import { Card, RangePicker } from '../components/common'

interface AuditLog {
  log_id: string
  timestamp: string
  event_type: string
  user_id: string | null
  case_id: string | null
  details: Record<string, any>
}

// Field names (matched case-insensitively, by substring) whose values are
// redacted before the details blob is rendered — checked against every
// gateway.log_audit_event() call site: cases.py writes the full case
// create/update payload (victim_info/suspect_info PII) into details.payload;
// case_assignments.py writes target_email; graph_retriever.py/xagg.py write
// free-text query strings that may echo investigation content.
const SENSITIVE_KEYS = ['payload', 'victim', 'suspect', 'email', 'query', 'cnic', 'phone', 'address', 'password']

function redact(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(redact)
  if (value && typeof value === 'object') {
    const out: Record<string, unknown> = {}
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
      out[k] = SENSITIVE_KEYS.some((s) => k.toLowerCase().includes(s)) ? '[redacted]' : redact(v)
    }
    return out
  }
  return value
}

function fmtDate(iso: string) {
  return new Date(iso).toLocaleString('en-PK', { hour12: true, dateStyle: 'medium', timeStyle: 'short' })
}

function eventBadgeClass(eventType: string): string {
  if (eventType === 'authorization_violation') return 'badge-error'
  if (eventType.startsWith('admin_')) return 'badge-accent'
  return ''
}

const PAGE_SIZE = 100

const AuditLogPage: React.FC = () => {
  const [logs, setLogs] = useState<AuditLog[]>([])
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState('')
  const [hasMore, setHasMore] = useState(true)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [days, setDays] = useState(30)

  // Text filters debounce into these before a request actually fires.
  const [eventTypeInput, setEventTypeInput] = useState('')
  const [caseIdInput, setCaseIdInput] = useState('')
  const [userIdInput, setUserIdInput] = useState('')
  const [eventType, setEventType] = useState('')
  const [caseId, setCaseId] = useState('')
  const [userId, setUserId] = useState('')

  useEffect(() => {
    const t = setTimeout(() => setEventType(eventTypeInput.trim()), 300)
    return () => clearTimeout(t)
  }, [eventTypeInput])
  useEffect(() => {
    const t = setTimeout(() => setCaseId(caseIdInput.trim()), 300)
    return () => clearTimeout(t)
  }, [caseIdInput])
  useEffect(() => {
    const t = setTimeout(() => setUserId(userIdInput.trim()), 300)
    return () => clearTimeout(t)
  }, [userIdInput])

  const fetchLogs = useCallback(async (offset: number, append: boolean) => {
    if (append) setLoadingMore(true)
    else setLoading(true)
    try {
      const res = await api.get<AuditLog[]>('/audit-logs', {
        params: {
          limit: PAGE_SIZE,
          offset,
          days,
          event_type: eventType || undefined,
          case_id: caseId || undefined,
          user_id: userId || undefined,
        },
      })
      setLogs((prev) => (append ? [...prev, ...res.data] : res.data))
      setHasMore(res.data.length === PAGE_SIZE)
      setError('')
    } catch (err: any) {
      setError(err?.response?.data?.detail || err.message || 'Failed to fetch logs')
      // Don't leave a stale, filter-mismatched result set on screen under
      // the error banner — a failed fetch means "we don't know", not
      // "here's what matched your new filter".
      if (!append) {
        setLogs([])
        setHasMore(false)
      }
    } finally {
      if (append) setLoadingMore(false)
      else setLoading(false)
    }
  }, [days, eventType, caseId, userId])

  useEffect(() => { fetchLogs(0, false) }, [fetchLogs])

  const hasFilters = eventTypeInput || caseIdInput || userIdInput

  return (
    <div className="main-content">
      <div className="page-header row-between">
        <div>
          <h1 className="page-title">Audit Logs</h1>
          <p className="page-sub">Chain of custody — every logged administrative and access-control event.</p>
        </div>
        <RangePicker value={days} onChange={(d) => setDays(d)} />
      </div>

      <div className="page-body">
        {error && (
          <div className="banner banner-warning">
            <span aria-hidden>⚠</span>
            <span><strong>Could not load audit logs.</strong> {error}</span>
          </div>
        )}

        <Card
          title="Events"
          sub={`${logs.length} shown${hasMore ? '+' : ''} · last ${days === 1 ? '24 hours' : `${days} days`}`}
          right={
            <button className="btn" onClick={() => fetchLogs(0, false)}>
              ↻ Refresh
            </button>
          }
        >
          <div className="filter-bar">
            <span className="filter-label">Event type</span>
            <input
              type="text"
              placeholder="e.g. admin_action"
              value={eventTypeInput}
              onChange={(e) => setEventTypeInput(e.target.value)}
              aria-label="Filter by event type"
            />
            <span className="filter-label">Case ID</span>
            <input
              type="text"
              placeholder="e.g. CASE-2026-014"
              value={caseIdInput}
              onChange={(e) => setCaseIdInput(e.target.value)}
              aria-label="Filter by case ID"
            />
            <span className="filter-label">User ID</span>
            <input
              type="text"
              placeholder="user UUID"
              value={userIdInput}
              onChange={(e) => setUserIdInput(e.target.value)}
              aria-label="Filter by user ID"
            />
            {hasFilters && (
              <button
                className="btn"
                onClick={() => { setEventTypeInput(''); setCaseIdInput(''); setUserIdInput('') }}
              >
                Clear
              </button>
            )}
          </div>

          {loading ? (
            <div className="loading-state"><div className="spinner" /><span>Loading…</span></div>
          ) : logs.length === 0 ? (
            <div className="empty-state">No logs found matching these criteria.</div>
          ) : (
            <>
              <div className="overflow-x-auto">
                <table>
                  <thead>
                    <tr>
                      <th>Timestamp</th>
                      <th>Event type</th>
                      <th>Case</th>
                      <th>User</th>
                    </tr>
                  </thead>
                  <tbody>
                    {logs.map((log) => (
                      <React.Fragment key={log.log_id}>
                        <tr
                          className="expand-row"
                          onClick={() => setExpanded(expanded === log.log_id ? null : log.log_id)}
                        >
                          <td style={{ whiteSpace: 'nowrap' }}>{fmtDate(log.timestamp)}</td>
                          <td>
                            <span className={`badge ${eventBadgeClass(log.event_type)}`}>{log.event_type}</span>
                          </td>
                          <td className="font-mono">{log.case_id || '—'}</td>
                          <td className="font-mono">{log.user_id ? `${log.user_id.slice(0, 8)}…` : 'System'}</td>
                        </tr>
                        {expanded === log.log_id && (
                          <tr>
                            <td colSpan={4} style={{ padding: 0 }}>
                              <div className="expand-panel font-mono">
                                {JSON.stringify(redact(log.details), null, 2)}
                              </div>
                            </td>
                          </tr>
                        )}
                      </React.Fragment>
                    ))}
                  </tbody>
                </table>
              </div>

              {hasMore && (
                <div style={{ textAlign: 'center', marginTop: 14 }}>
                  <button className="btn" disabled={loadingMore} onClick={() => fetchLogs(logs.length, true)}>
                    {loadingMore ? 'Loading…' : 'Load more'}
                  </button>
                </div>
              )}
            </>
          )}
        </Card>
      </div>
    </div>
  )
}

export default AuditLogPage
