// ============================================================
// Errors — a filterable history, with a trend so you can see whether
// something is getting worse rather than just that it happened.
// ============================================================

import React, { useEffect, useMemo, useState } from 'react'
import api from '../api'
import { TrendChart, NoData, formatNumber } from '../components/charts'
import {
  Card, StatCard, RangePicker, InstrumentationBanner, StatusBadge, formatWhen,
} from '../components/common'

interface ErrorRow {
  error_id: string
  occurred_at: string
  severity: string
  error_type: string | null
  module: string | null
  message: string
  stack_trace: string | null
  run_id: string | null
  session_id: string | null
}

interface Facets {
  modules: string[]
  error_types: string[]
  severities: string[]
}

const ErrorsPage: React.FC = () => {
  const [days, setDays] = useState(7)
  const [granularity, setGranularity] = useState<'day' | 'hour'>('day')
  const [severity, setSeverity] = useState('')
  const [module, setModule] = useState('')
  const [errorType, setErrorType] = useState('')
  const [search, setSearch] = useState('')

  const [rows, setRows] = useState<ErrorRow[]>([])
  const [facets, setFacets] = useState<Facets>({ modules: [], error_types: [], severities: [] })
  const [trend, setTrend] = useState<Array<Record<string, number | string>>>([])
  const [total, setTotal] = useState(0)
  const [missing, setMissing] = useState<string[]>([])
  const [expanded, setExpanded] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    setLoading(true)

    Promise.all([
      api.get<{ errors: ErrorRow[]; facets: Facets }>('/errors', {
        params: {
          days, limit: 200,
          severity: severity || undefined,
          module: module || undefined,
          error_type: errorType || undefined,
        },
      }),
      api.get<{ total: number; timeseries: Array<Record<string, number | string>> }>(
        '/errors/trend', { params: { days, granularity } },
      ),
      api.get<{ tables: Record<string, boolean> }>('/instrumentation'),
    ])
      .then(([list, trendRes, inst]) => {
        if (cancelled) return
        setRows(list.data.errors)
        setFacets(list.data.facets)
        setTrend(trendRes.data.timeseries)
        setTotal(trendRes.data.total)
        setMissing(
          Object.entries(inst.data.tables)
            .filter(([, present]) => !present)
            .map(([name]) => name),
        )
      })
      .finally(() => !cancelled && setLoading(false))

    return () => { cancelled = true }
  }, [days, granularity, severity, module, errorType])

  // Free-text search is applied client-side: the message column is the one
  // field a regex-y search actually helps with, and the page holds ≤200 rows.
  const visible = useMemo(() => {
    if (!search.trim()) return rows
    const needle = search.toLowerCase()
    return rows.filter(
      (r) =>
        r.message.toLowerCase().includes(needle) ||
        (r.module ?? '').toLowerCase().includes(needle) ||
        (r.error_type ?? '').toLowerCase().includes(needle),
    )
  }, [rows, search])

  const criticalCount = rows.filter((r) => r.severity === 'critical').length
  const topModule = useMemo(() => {
    const counts = new Map<string, number>()
    rows.forEach((r) => counts.set(r.module ?? '—', (counts.get(r.module ?? '—') ?? 0) + 1))
    return [...counts.entries()].sort((a, b) => b[1] - a[1])[0]
  }, [rows])

  return (
    <div className="main-content">
      <div className="page-header row-between">
        <div>
          <h1 className="page-title">Errors</h1>
          <p className="page-sub">
            Every ERROR and CRITICAL the backend logged, with a trend and full stack traces.
          </p>
        </div>
        <RangePicker value={days} onChange={(d, g) => { setDays(d); setGranularity(g) }} />
      </div>

      <div className="page-body">
        <InstrumentationBanner missing={missing.filter((m) => m === 'error_logs')} />

        <div className="stat-grid">
          <StatCard
            label="Errors"
            value={formatNumber(total)}
            hint={`in the last ${days === 1 ? '24 hours' : `${days} days`}`}
            tone={total > 0 ? 'bad' : 'good'}
          />
          <StatCard
            label="Critical"
            value={formatNumber(criticalCount)}
            hint={criticalCount > 0 ? 'needs attention' : 'none'}
            tone={criticalCount > 0 ? 'bad' : 'good'}
          />
          <StatCard
            label="Noisiest module"
            value={topModule ? String(topModule[1]) : '0'}
            hint={topModule ? topModule[0] : '—'}
          />
          <StatCard label="Distinct types" value={facets.error_types.length} hint="exception classes seen" />
        </div>

        <Card title="Errors over time" sub="By severity">
          {total > 0 ? (
            <TrendChart
              data={trend}
              series={[
                { key: 'error', name: 'Error' },
                { key: 'critical', name: 'Critical' },
                { key: 'warning', name: 'Warning' },
              ]}
            />
          ) : (
            <NoData
              label={
                missing.includes('error_logs')
                  ? 'Errors are not being recorded yet — run migration 003'
                  : 'No errors in this period — nothing to plot'
              }
            />
          )}
        </Card>

        <Card
          title="Error history"
          sub={`${visible.length} of ${rows.length} shown · click a row for the stack trace`}
        >
          <div className="filter-bar">
            <span className="filter-label">Severity</span>
            <select value={severity} onChange={(e) => setSeverity(e.target.value)}>
              <option value="">All</option>
              {facets.severities.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>

            <span className="filter-label">Module</span>
            <select value={module} onChange={(e) => setModule(e.target.value)}>
              <option value="">All</option>
              {facets.modules.map((m) => <option key={m} value={m}>{m}</option>)}
            </select>

            <span className="filter-label">Type</span>
            <select value={errorType} onChange={(e) => setErrorType(e.target.value)}>
              <option value="">All</option>
              {facets.error_types.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>

            <input
              type="search"
              placeholder="Search messages…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              style={{ minWidth: 200, marginLeft: 'auto' }}
            />

            {(severity || module || errorType || search) && (
              <button
                className="btn"
                onClick={() => { setSeverity(''); setModule(''); setErrorType(''); setSearch('') }}
              >
                Clear
              </button>
            )}
          </div>

          {loading ? (
            <div className="loading-state">Loading…</div>
          ) : visible.length === 0 ? (
            <div className="empty-state">
              {rows.length === 0 ? 'No errors recorded in this period.' : 'No errors match these filters.'}
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table>
                <thead>
                  <tr>
                    <th>When</th>
                    <th>Severity</th>
                    <th>Type</th>
                    <th>Module</th>
                    <th>Message</th>
                  </tr>
                </thead>
                <tbody>
                  {visible.map((row) => (
                    <React.Fragment key={row.error_id}>
                      <tr
                        className="expand-row"
                        onClick={() => setExpanded(expanded === row.error_id ? null : row.error_id)}
                      >
                        <td style={{ whiteSpace: 'nowrap' }}>{formatWhen(row.occurred_at)}</td>
                        <td><StatusBadge status={row.severity} /></td>
                        <td className="font-mono">{row.error_type ?? '—'}</td>
                        <td className="font-mono">{row.module ?? '—'}</td>
                        <td className="truncate" title={row.message}>{row.message}</td>
                      </tr>
                      {expanded === row.error_id && (
                        <tr>
                          <td colSpan={5} style={{ padding: 0 }}>
                            <div className="expand-panel font-mono">
                              {row.stack_trace || row.message}
                              {row.run_id && (
                                <div style={{ marginTop: 8, color: 'var(--text-faint)' }}>
                                  run {row.run_id}
                                </div>
                              )}
                            </div>
                          </td>
                        </tr>
                      )}
                    </React.Fragment>
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

export default ErrorsPage
