// ============================================================
// Dashboard — the one screen that answers "is Muhafiz healthy?"
//
// Reading order, top to bottom: totals at a glance → what the traffic is doing
// → how fast → what broke. Everything is computed from real rows; nothing here
// is placeholder.
// ============================================================

import React, { useEffect, useState } from 'react'
import api from '../api'
import {
  TrendChart, VolumeChart, BreakdownChart, ShareChart, NoData,
  formatMs, formatNumber,
} from '../components/charts'
import {
  Card, StatCard, RangePicker, InstrumentationBanner,
} from '../components/common'

interface UsagePayload {
  total_requests: number
  timeseries: Array<Record<string, number | string>>
  routing: Array<{ route: string; count: number; share_pct: number; success_pct: number; avg_ms: number; p95_ms: number }>
}

interface LatencyPayload {
  summary: { count: number; avg_ms: number; p50_ms: number; p95_ms: number; max_ms: number }
  timeseries: Array<Record<string, number | string>>
  by_step: Array<{ step_name: string; count: number; avg_ms: number; p50_ms: number; p95_ms: number; max_ms: number; failures: number }>
}

interface KbPayload {
  total_chunks: number
  total_documents: number
  documents: Array<{ doc_id: string; filename: string; chunk_count: number }>
}

interface ErrorTrendPayload {
  total: number
  timeseries: Array<Record<string, number | string>>
}

interface VerifierPayload {
  total_gated: number
  passed: number
  regenerated: number
  pass_rate_pct: number
  regeneration_rate_pct: number
}

const STEP_LABELS: Record<string, string> = {
  query_rewriter: 'Query rewriter',
  router: 'Router',
  retrieval: 'Retrieval',
  web_search: 'Web search',
  reranker: 'Re-ranker',
  evaluator: 'Evaluator',
  response: 'Response',
  file_generation: 'File generation',
  memory: 'Memory',
}

const DashboardPage: React.FC = () => {
  const [days, setDays] = useState(7)
  const [granularity, setGranularity] = useState<'day' | 'hour'>('day')
  const [usage, setUsage] = useState<UsagePayload | null>(null)
  const [latency, setLatency] = useState<LatencyPayload | null>(null)
  const [kb, setKb] = useState<KbPayload | null>(null)
  const [errorTrend, setErrorTrend] = useState<ErrorTrendPayload | null>(null)
  const [verifier, setVerifier] = useState<VerifierPayload | null>(null)
  const [missing, setMissing] = useState<string[]>([])
  const [loading, setLoading] = useState(true)
  const [failure, setFailure] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setFailure(null)

    const params = { params: { days, granularity } }

    Promise.all([
      api.get<UsagePayload>('/usage', params),
      api.get<LatencyPayload>('/latency', params),
      api.get<KbPayload>('/kb/stats'),
      api.get<ErrorTrendPayload>('/errors/trend', params),
      api.get<VerifierPayload>('/verifier/stats', params),
      api.get<{ tables: Record<string, boolean> }>('/instrumentation'),
    ])
      .then(([u, l, k, e, v, inst]) => {
        if (cancelled) return
        setUsage(u.data)
        setLatency(l.data)
        setKb(k.data)
        setErrorTrend(e.data)
        setVerifier(v.data)
        setMissing(
          Object.entries(inst.data.tables)
            .filter(([, present]) => !present)
            .map(([name]) => name),
        )
      })
      .catch((err) => {
        if (!cancelled) setFailure(err?.response?.data?.detail ?? 'Failed to load dashboard data')
      })
      .finally(() => !cancelled && setLoading(false))

    return () => { cancelled = true }
  }, [days, granularity])

  if (loading && !usage) {
    return (
      <div className="main-content">
        <div className="loading-state">Loading dashboard…</div>
      </div>
    )
  }

  if (failure) {
    return (
      <div className="main-content">
        <div className="banner banner-warning">
          <span aria-hidden>⚠</span>
          <span><strong>Could not load the dashboard.</strong> {failure}</span>
        </div>
      </div>
    )
  }

  const routes = usage?.routing ?? []
  const steps = (latency?.by_step ?? []).map((s) => ({
    ...s,
    label: STEP_LABELS[s.step_name] ?? s.step_name,
  }))
  const slowest = steps[0]
  const summary = latency?.summary

  // Only plot route series that actually occurred in the window.
  const routeKeys = routes.map((r) => r.route)

  return (
    <div className="main-content">
      <div className="page-header row-between">
        <div>
          <h1 className="page-title">Dashboard</h1>
          <p className="page-sub">
            Live system health — requests, routing, latency, errors and the knowledge base.
          </p>
        </div>
        <RangePicker
          value={days}
          onChange={(d, g) => { setDays(d); setGranularity(g) }}
        />
      </div>

      <div className="page-body">
        <InstrumentationBanner missing={missing} />

        {/* ── Totals at a glance ── */}
        <div className="stat-grid">
          <StatCard
            label="Requests"
            value={formatNumber(usage?.total_requests ?? 0)}
            hint={`in the last ${days === 1 ? '24 hours' : `${days} days`}`}
          />
          <StatCard
            label="Median latency"
            value={formatMs(summary?.p50_ms ?? 0)}
            hint={`p95 ${formatMs(summary?.p95_ms ?? 0)}`}
            tone={(summary?.p95_ms ?? 0) > 15000 ? 'bad' : 'neutral'}
          />
          <StatCard
            label="Chunks indexed"
            value={formatNumber(kb?.total_chunks ?? 0)}
            hint={`across ${formatNumber(kb?.total_documents ?? 0)} documents`}
          />
          <StatCard
            label="Errors"
            value={formatNumber(errorTrend?.total ?? 0)}
            hint={missing.includes('error_logs') ? 'not being recorded yet' : 'logged in this period'}
            tone={(errorTrend?.total ?? 0) > 0 ? 'bad' : 'good'}
          />
          <StatCard
            label="Slowest step"
            value={slowest ? formatMs(slowest.avg_ms) : '—'}
            hint={slowest ? slowest.label : 'no steps recorded'}
          />
          <StatCard
            label="Grounding pass rate"
            value={verifier && verifier.total_gated > 0 ? `${verifier.pass_rate_pct}%` : '—'}
            hint={verifier && verifier.total_gated > 0 ? `of ${formatNumber(verifier.total_gated)} gated responses` : 'no gated responses'}
            tone={verifier && verifier.total_gated > 0 && verifier.pass_rate_pct < 80 ? 'bad' : 'good'}
          />
        </div>

        {/* ── Traffic ── */}
        <div className="charts-row wide">
          <Card title="Requests over time" sub="Total pipeline runs per bucket">
            {usage && usage.total_requests > 0 ? (
              <VolumeChart
                data={usage.timeseries}
                series={[{ key: 'total', name: 'Requests' }]}
              />
            ) : (
              <NoData />
            )}
          </Card>

          <Card title="Routing" sub="How requests were handled">
            {routes.length > 0 ? (
              <ShareChart data={routes} nameKey="route" valueKey="count" />
            ) : (
              <NoData />
            )}
          </Card>
        </div>

        {/* Requests split by route — the "by intent/tool" view */}
        <Card
          title="Requests by route"
          sub="RAG searches the knowledge base · SQL hits the rate tables · WEB searches the web · DIRECT answers without retrieval"
        >
          {routes.length > 0 && usage ? (
            <>
              <TrendChart
                data={usage.timeseries}
                series={routeKeys.map((route) => ({ key: route, name: route }))}
              />
              <div className="overflow-x-auto mt-6">
                <table>
                  <thead>
                    <tr>
                      <th>Route</th>
                      <th className="num">Requests</th>
                      <th className="num">Share</th>
                      <th className="num">Answered</th>
                      <th className="num">Avg</th>
                      <th className="num">p95</th>
                    </tr>
                  </thead>
                  <tbody>
                    {routes.map((r) => (
                      <tr key={r.route}>
                        <td><span className="badge badge-accent">{r.route}</span></td>
                        <td className="num">{formatNumber(r.count)}</td>
                        <td className="num">{r.share_pct}%</td>
                        <td className="num">{r.success_pct}%</td>
                        <td className="num">{formatMs(r.avg_ms)}</td>
                        <td className="num">{formatMs(r.p95_ms)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          ) : (
            <NoData />
          )}
        </Card>

        {/* ── Latency ── */}
        <div className="charts-row">
          <Card title="Latency trend" sub="Average and p95 per bucket">
            {summary && summary.count > 0 && latency ? (
              <TrendChart
                data={latency.timeseries}
                series={[
                  { key: 'avg_ms', name: 'Average' },
                  { key: 'p95_ms', name: 'p95' },
                ]}
                valueFormatter={formatMs}
              />
            ) : (
              <NoData />
            )}
          </Card>

          <Card title="Where the time goes" sub="Average duration per pipeline step">
            {steps.length > 0 ? (
              <BreakdownChart
                data={steps}
                categoryKey="label"
                valueKey="avg_ms"
                valueFormatter={formatMs}
              />
            ) : (
              <NoData />
            )}
          </Card>
        </div>

        <Card title="Pipeline steps" sub="Per-step latency across the selected period">
          {steps.length > 0 ? (
            <div className="overflow-x-auto">
              <table>
                <thead>
                  <tr>
                    <th>Step</th>
                    <th className="num">Runs</th>
                    <th className="num">Avg</th>
                    <th className="num">p50</th>
                    <th className="num">p95</th>
                    <th className="num">Max</th>
                    <th className="num">Failures</th>
                  </tr>
                </thead>
                <tbody>
                  {steps.map((s) => (
                    <tr key={s.step_name}>
                      <td className="font-semibold" style={{ color: 'var(--text-primary)' }}>{s.label}</td>
                      <td className="num">{formatNumber(s.count)}</td>
                      <td className="num">{formatMs(s.avg_ms)}</td>
                      <td className="num">{formatMs(s.p50_ms)}</td>
                      <td className="num">{formatMs(s.p95_ms)}</td>
                      <td className="num">{formatMs(s.max_ms)}</td>
                      <td className="num">
                        {s.failures > 0
                          ? <span className="badge badge-failed">{s.failures}</span>
                          : <span className="text-muted">0</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <NoData />
          )}
        </Card>

        {/* ── Errors + knowledge base ── */}
        <div className="charts-row">
          <Card title="Errors over time" sub="By severity">
            {errorTrend && errorTrend.total > 0 ? (
              <TrendChart
                data={errorTrend.timeseries}
                series={[
                  { key: 'error', name: 'Error' },
                  { key: 'critical', name: 'Critical' },
                ]}
              />
            ) : (
              <NoData
                label={
                  missing.includes('error_logs')
                    ? 'Errors are not being recorded yet — run migration 003'
                    : 'No errors in this period'
                }
              />
            )}
          </Card>

          <Card title="Largest documents" sub="Chunks per document (top 8)">
            {kb && kb.documents.length > 0 ? (
              <BreakdownChart
                data={kb.documents.slice(0, 8).map((d) => ({
                  ...d,
                  label: d.filename.length > 22 ? `${d.filename.slice(0, 20)}…` : d.filename,
                }))}
                categoryKey="label"
                valueKey="chunk_count"
              />
            ) : (
              <NoData label="No documents indexed" />
            )}
          </Card>
        </div>
      </div>
    </div>
  )
}

export default DashboardPage
