import React, { useEffect, useState } from 'react'
import api from '../api'

interface McpCall {
  call_id: string
  run_id: string
  mcp_server: string
  tool_name: string
  input_params: Record<string, any> | null
  status: string
  error_message: string | null
  started_at: string
  completed_at: string | null
  run?: { original_query: string } | null
}

function fmtDate(iso: string) {
  return new Date(iso).toLocaleString('en-PK', { hour12: true, dateStyle: 'short', timeStyle: 'short' })
}

const McpCallLogPage: React.FC = () => {
  const [calls, setCalls] = useState<McpCall[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const fetchCalls = async () => {
    setLoading(true)
    setError('')
    try {
      const res = await api.get('/mcp-calls', { params: { limit: 100 } })
      setCalls(res.data)
    } catch {
      setError('Failed to load MCP calls')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { fetchCalls() }, [])

  return (
    <div className="main-content">
      <div className="page-header">
        <div>
          <div className="page-title">MCP Call Log</div>
          <div className="page-subtitle">History of Model Context Protocol server interactions</div>
        </div>
        <button className="btn btn-primary" onClick={fetchCalls} style={{ fontSize: 12 }}>
          ↻ Refresh
        </button>
      </div>

      <div className="page-body">
        <div className="table-wrapper">
          {loading && <div className="loading-state"><div className="spinner"/><span>Loading…</span></div>}
          {error && <div className="loading-state" style={{ color: 'var(--error)' }}>{error}</div>}

          {!loading && !error && (
            calls.length === 0 ? (
              <div className="empty-state">
                <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.5} style={{ color: 'var(--text-muted)' }}>
                  <path d="M8 9l3 3-3 3m5 0h3M4 6a2 2 0 012-2h12a2 2 0 012 2v12a2 2 0 01-2 2H6a2 2 0 01-2-2V6z"/>
                </svg>
                <span>No MCP calls recorded yet.</span>
                <span style={{ fontSize: 13, marginTop: 8 }}>The SQL/MCP connection block might be active, or no runs have triggered MCP tools.</span>
              </div>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th>Started At</th>
                    <th>Server / Tool</th>
                    <th>Status</th>
                    <th>Original Query</th>
                    <th>Params</th>
                    <th>Error</th>
                  </tr>
                </thead>
                <tbody>
                  {calls.map(c => (
                    <tr key={c.call_id}>
                      <td style={{ whiteSpace: 'nowrap' }}>{fmtDate(c.started_at)}</td>
                      <td>
                        <div style={{ fontWeight: 600 }}>{c.mcp_server}</div>
                        <div style={{ color: 'var(--text-muted)', fontSize: 12 }}>{c.tool_name}</div>
                      </td>
                      <td>
                        <span className={`badge ${c.status === 'success' ? 'badge-success' : c.status === 'failed' ? 'badge-error' : 'badge-neutral'}`}>
                          {c.status}
                        </span>
                      </td>
                      <td>{c.run?.original_query ? c.run.original_query : <span className="text-muted">N/A</span>}</td>
                      <td>
                        {c.input_params ? (
                          <pre style={{ fontSize: 11, background: 'var(--surface-2)', padding: 4, borderRadius: 4, margin: 0, maxWidth: 200, overflowX: 'auto' }}>
                            {JSON.stringify(c.input_params, null, 2)}
                          </pre>
                        ) : '—'}
                      </td>
                      <td style={{ color: 'var(--error)', maxWidth: 200, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }} title={c.error_message || ''}>
                        {c.error_message || '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )
          )}
        </div>
      </div>
    </div>
  )
}

export default McpCallLogPage
