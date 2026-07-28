import React, { useEffect, useState } from 'react'
import api from '../api'
import { Card, StatCard, formatWhen } from '../components/common'

interface TierMetrics {
  correct: number
  total: number
  precision: number | null
}

interface TestCase {
  name: string
  passed: boolean
  detail: string
}

interface EvalMetrics {
  generated_at: string | null
  total_mentions_processed: number
  total_entities_processed: number
  test_cases_passed: number
  test_cases_total: number
  test_cases: TestCase[]
  tier_metrics: Record<string, TierMetrics>
  error?: string
}

const EntityEvalPage: React.FC = () => {
  const [data, setData] = useState<EvalMetrics | null>(null)
  const [loading, setLoading] = useState(true)
  const [failure, setFailure] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    api.get<EvalMetrics>('/eval/entity-resolution')
      .then((res) => {
        if (cancelled) return
        setData(res.data)
      })
      .catch((err) => {
        if (!cancelled) setFailure(err?.response?.data?.detail ?? 'Failed to load entity-resolution metrics')
      })
      .finally(() => !cancelled && setLoading(false))
    return () => { cancelled = true }
  }, [])

  if (loading) {
    return (
      <div className="main-content">
        <div className="loading-state"><div className="spinner" /><span>Loading metrics…</span></div>
      </div>
    )
  }

  if (failure || !data) {
    return (
      <div className="main-content">
        <div className="banner banner-warning">
          <span aria-hidden>⚠</span>
          <span><strong>Could not load entity-resolution metrics.</strong> {failure || 'No data returned.'}</span>
        </div>
      </div>
    )
  }

  if (data.error) {
    return (
      <div className="main-content">
        <div className="page-header">
          <div className="page-title">Entity Resolution Evaluation</div>
        </div>
        <div className="page-body">
          <Card title="Entity Resolution Eval">
            <div style={{ color: 'var(--error)' }}>{data.error}</div>
          </Card>
        </div>
      </div>
    )
  }

  return (
    <div className="main-content">
      <div className="page-header">
        <div>
          <div className="page-title">Entity Resolution Evaluation</div>
          <p className="page-sub">
            Ground truth metrics against the Phase 3.6 test set — metrics as of {formatWhen(data.generated_at)}.
          </p>
        </div>
      </div>

      <div className="page-body">
        <div className="stat-grid">
          <StatCard label="Mentions processed" value={data.total_mentions_processed} />
          <StatCard label="Canonical entities" value={data.total_entities_processed} />
          <StatCard
            label="Invariants passed"
            value={`${data.test_cases_passed} / ${data.test_cases_total}`}
            tone={data.test_cases_passed < data.test_cases_total ? 'bad' : 'good'}
          />
        </div>

        <Card title="Precision by tier" sub="Recall is explicitly bounded by the 37-entity roster size and not estimated here.">
          <div className="overflow-x-auto">
            <table>
              <thead>
                <tr>
                  <th>Tier</th>
                  <th className="num">Correct</th>
                  <th className="num">Total</th>
                  <th className="num">Precision</th>
                </tr>
              </thead>
              <tbody>
                {data.tier_metrics && Object.entries(data.tier_metrics).map(([tier, metrics]) => (
                  <tr key={tier}>
                    <td style={{ fontWeight: 500, color: 'var(--accent)' }}>{tier}</td>
                    <td className="num">{metrics.correct}</td>
                    <td className="num">{metrics.total}</td>
                    <td className="num">
                      {metrics.precision !== null ? `${(metrics.precision * 100).toFixed(1)}%` : 'N/A'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>

        <Card title="Hard invariant test cases">
          <div className="overflow-x-auto">
            <table>
              <thead>
                <tr>
                  <th style={{ width: 40 }}>Result</th>
                  <th>Test case</th>
                  <th>Detail</th>
                </tr>
              </thead>
              <tbody>
                {data.test_cases?.map((tc, idx) => (
                  <tr key={idx}>
                    <td style={{ textAlign: 'center', color: tc.passed ? 'var(--success)' : 'var(--error)' }}>
                      {tc.passed ? '✓' : '✗'}
                    </td>
                    <td style={{ fontWeight: 500 }}>{tc.name}</td>
                    <td style={{ color: 'var(--text-muted)', fontSize: 12 }}>{tc.detail}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      </div>
    </div>
  )
}

export default EntityEvalPage
