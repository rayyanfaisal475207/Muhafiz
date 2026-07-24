// ============================================================
// Chart primitives — thin wrappers over Recharts.
//
// One place decides how a Muhafiz chart looks: token-driven colours, a light
// horizontal-only grid, tabular numbers, and a tooltip that matches the app's
// surfaces. Pages describe *what* to plot, never *how*.
// ============================================================

import {
  Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, Legend, Line, LineChart,
  Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'

export const SERIES_COLORS = [
  'var(--chart-1)', 'var(--chart-2)', 'var(--chart-3)',
  'var(--chart-4)', 'var(--chart-5)', 'var(--chart-6)',
]

/** Charts are read left-to-right; long ISO buckets are noise. */
function formatBucket(value: string): string {
  if (!value) return ''
  if (value.includes('T')) {
    const [date, time] = value.split('T')
    return `${date.slice(5)} ${time.slice(0, 2)}h`
  }
  return value.slice(5)  // MM-DD
}

export function formatMs(ms: number): string {
  if (!ms) return '0ms'
  return ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(1)}s`
}

export function formatNumber(n: number): string {
  return (n ?? 0).toLocaleString()
}

interface TooltipProps {
  active?: boolean
  payload?: Array<{ name: string; value: number; color?: string; dataKey?: string }>
  label?: string
  valueFormatter?: (v: number) => string
}

function ChartTooltip({ active, payload, label, valueFormatter }: TooltipProps) {
  if (!active || !payload?.length) return null
  const fmt = valueFormatter ?? formatNumber
  return (
    <div className="chart-tooltip">
      <div className="label">{formatBucket(String(label ?? ''))}</div>
      {payload.map((entry) => (
        <div className="row" key={entry.dataKey ?? entry.name}>
          <span className="swatch" style={{ background: entry.color }} />
          <span>{entry.name}</span>
          <strong style={{ marginLeft: 'auto', color: 'var(--text-primary)' }}>
            {fmt(entry.value)}
          </strong>
        </div>
      ))}
    </div>
  )
}

const AXIS = {
  stroke: 'var(--border-strong)',
  tickLine: false,
  axisLine: false,
} as const

interface TrendProps {
  data: Array<Record<string, unknown>>
  series: Array<{ key: string; name: string }>
  valueFormatter?: (v: number) => string
  height?: string
}

/** Line chart for a trend over time. */
export function TrendChart({ data, series, valueFormatter, height = '' }: TrendProps) {
  return (
    <div className={`chart-shell ${height}`}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 6, right: 8, left: -12, bottom: 0 }}>
          {/* Horizontal lines only: vertical gridlines add ink without adding meaning. */}
          <CartesianGrid stroke="var(--chart-grid)" vertical={false} />
          <XAxis dataKey="bucket" tickFormatter={formatBucket} {...AXIS} />
          <YAxis {...AXIS} width={52} tickFormatter={(v) => (valueFormatter ? valueFormatter(v) : formatNumber(v))} />
          <Tooltip content={<ChartTooltip valueFormatter={valueFormatter} />} />
          {series.length > 1 && <Legend iconType="plainline" wrapperStyle={{ fontSize: 12 }} />}
          {series.map((s, i) => (
            <Line
              key={s.key}
              type="monotone"
              dataKey={s.key}
              name={s.name}
              stroke={SERIES_COLORS[i % SERIES_COLORS.length]}
              strokeWidth={2}
              dot={false}
              activeDot={{ r: 3.5 }}
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}

/** Filled area chart — used for volume, where the quantity beneath matters. */
export function VolumeChart({ data, series, valueFormatter, height = '' }: TrendProps) {
  return (
    <div className={`chart-shell ${height}`}>
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 6, right: 8, left: -12, bottom: 0 }}>
          <defs>
            {series.map((s, i) => (
              <linearGradient key={s.key} id={`fill-${s.key}`} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={SERIES_COLORS[i % SERIES_COLORS.length]} stopOpacity={0.22} />
                <stop offset="100%" stopColor={SERIES_COLORS[i % SERIES_COLORS.length]} stopOpacity={0.02} />
              </linearGradient>
            ))}
          </defs>
          <CartesianGrid stroke="var(--chart-grid)" vertical={false} />
          <XAxis dataKey="bucket" tickFormatter={formatBucket} {...AXIS} />
          <YAxis {...AXIS} width={52} tickFormatter={(v) => (valueFormatter ? valueFormatter(v) : formatNumber(v))} />
          <Tooltip content={<ChartTooltip valueFormatter={valueFormatter} />} />
          {series.length > 1 && <Legend iconType="plainline" wrapperStyle={{ fontSize: 12 }} />}
          {series.map((s, i) => (
            <Area
              key={s.key}
              type="monotone"
              dataKey={s.key}
              name={s.name}
              stroke={SERIES_COLORS[i % SERIES_COLORS.length]}
              strokeWidth={2}
              fill={`url(#fill-${s.key})`}
            />
          ))}
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}

interface BreakdownProps {
  data: Array<Record<string, unknown>>
  categoryKey: string
  valueKey: string
  valueFormatter?: (v: number) => string
  height?: string
  layout?: 'horizontal' | 'vertical'
}

/** Bar chart for a breakdown across categories. */
export function BreakdownChart({
  data, categoryKey, valueKey, valueFormatter, height = '', layout = 'vertical',
}: BreakdownProps) {
  const isVertical = layout === 'vertical'  // horizontal bars, category on the Y axis
  return (
    <div className={`chart-shell ${height}`}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart
          data={data}
          layout={isVertical ? 'vertical' : 'horizontal'}
          margin={{ top: 6, right: 16, left: isVertical ? 8 : -12, bottom: 0 }}
        >
          <CartesianGrid stroke="var(--chart-grid)" horizontal={!isVertical} vertical={isVertical} />
          {isVertical ? (
            <>
              <XAxis
                type="number"
                {...AXIS}
                tickFormatter={(v) => (valueFormatter ? valueFormatter(v) : formatNumber(v))}
              />
              <YAxis type="category" dataKey={categoryKey} {...AXIS} width={130} />
            </>
          ) : (
            <>
              <XAxis dataKey={categoryKey} {...AXIS} />
              <YAxis
                {...AXIS}
                width={52}
                tickFormatter={(v) => (valueFormatter ? valueFormatter(v) : formatNumber(v))}
              />
            </>
          )}
          <Tooltip
            cursor={{ fill: 'var(--accent-soft)' }}
            content={<ChartTooltip valueFormatter={valueFormatter} />}
          />
          <Bar dataKey={valueKey} radius={isVertical ? [0, 4, 4, 0] : [4, 4, 0, 0]} maxBarSize={26}>
            {data.map((_, i) => (
              <Cell key={i} fill={SERIES_COLORS[i % SERIES_COLORS.length]} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

interface ShareProps {
  data: Array<Record<string, unknown>>
  nameKey: string
  valueKey: string
  height?: string
}

/** Donut for proportions — a whole split into a handful of parts. */
export function ShareChart({ data, nameKey, valueKey, height = '' }: ShareProps) {
  return (
    <div className={`chart-shell ${height}`}>
      <ResponsiveContainer width="100%" height="100%">
        <PieChart>
          <Pie
            data={data}
            dataKey={valueKey}
            nameKey={nameKey}
            innerRadius="52%"
            outerRadius="78%"
            paddingAngle={2}
            stroke="var(--bg-surface)"
            strokeWidth={2}
          >
            {data.map((_, i) => (
              <Cell key={i} fill={SERIES_COLORS[i % SERIES_COLORS.length]} />
            ))}
          </Pie>
          <Tooltip content={<ChartTooltip />} />
          <Legend
            verticalAlign="bottom"
            iconType="circle"
            wrapperStyle={{ fontSize: 12, color: 'var(--text-secondary)' }}
          />
        </PieChart>
      </ResponsiveContainer>
    </div>
  )
}

/** Shown in place of a chart when the window genuinely contains no data. */
export function NoData({ label = 'No data in this period' }: { label?: string }) {
  return <div className="empty-state">{label}</div>
}
