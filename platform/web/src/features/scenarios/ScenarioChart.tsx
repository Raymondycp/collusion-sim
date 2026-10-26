import { useEffect, useRef } from 'react'
import {
  ColorType,
  createChart,
  createSeriesMarkers,
  HistogramSeries,
  LineSeries,
  LineStyle,
  LineType,
  type IChartApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from 'lightweight-charts'
import { cssVar, useTheme } from '@/app/theme'
import { SELLER_COLORS } from '@/components/chart/PriceChart'
import type { ReferencePoint, ScenarioMarker } from '@/api/scenarios'

// Same time axis as the run view: one round is one day.
const DAY = 86_400
const toTime = (t: number) => (t * DAY) as UTCTimestamp
const roundLabel = (time: Time) => `R${Math.round((time as number) / DAY)}`

type Point = { t: number; value: number }
export type ScenarioSeries = {
  nash: Point[]
  joint: Point[]
  params: { name: string; points: Point[] }[]
  /** Rounds inside the quiet window, drawn as a band. */
  quiet: number[]
  markers: { t: number; text: string }[]
}

const MARK: Record<ScenarioMarker['kind'], string> = { announce: 'News', effect: 'Shock', end: 'End' }

/** params(t), Nash(t), joint optimum(t), the quiet window and the event markers, ready to draw. A param that never changes is left out. */
export function scenarioSeries(rounds: ReferencePoint[], markers: ScenarioMarker[], quietWindow?: number[]): ScenarioSeries {
  const num = (r: ReferencePoint, k: string) => (typeof r.params[k] === 'number' ? (r.params[k] as number) : null)
  const names = [...new Set(rounds.flatMap((r) => Object.keys(r.params)))].filter((k) => {
    const vals = rounds.map((r) => num(r, k)).filter((v) => v !== null)
    return vals.length > 0 && new Set(vals).size > 1
  })
  const [lo, hi] = quietWindow?.length === 2 ? quietWindow : [Infinity, -Infinity]
  return {
    nash: rounds.flatMap((r) => (r.nash == null ? [] : [{ t: r.t, value: r.nash }])),
    joint: rounds.flatMap((r) => (r.joint_optimum == null ? [] : [{ t: r.t, value: r.joint_optimum }])),
    params: names.map((name) => ({ name, points: rounds.flatMap((r) => (num(r, name) === null ? [] : [{ t: r.t, value: num(r, name)! }])) })),
    quiet: rounds.map((r) => r.t).filter((t) => t >= lo! && t <= hi!),
    markers: [...markers].sort((a, b) => a.t - b.t).map((m) => ({ t: m.t, text: `${MARK[m.kind]} ${m.event_index + 1}` })),
  }
}

export function ScenarioChart({ rounds, markers, quietWindow }: { rounds: ReferencePoint[]; markers: ScenarioMarker[]; quietWindow?: number[] }) {
  const host = useRef<HTMLDivElement>(null)
  const chart = useRef<IChartApi | null>(null)
  const theme = useTheme()
  const data = scenarioSeries(rounds, markers, quietWindow)

  useEffect(() => {
    const c = createChart(host.current!, {
      autoSize: true,
      layout: { attributionLogo: true, background: { type: ColorType.Solid, color: 'transparent' }, fontFamily: 'Geist Variable, sans-serif' },
      localization: { timeFormatter: roundLabel, priceFormatter: (p: number) => p.toFixed(2) },
      timeScale: { tickMarkFormatter: roundLabel, fixLeftEdge: true, rightOffset: 2 },
      crosshair: { mode: 0 },
    })
    chart.current = c
    return () => {
      c.remove()
      chart.current = null
    }
  }, [])

  // Redraw everything on each change: a scenario has at most 200 rounds and a few series.
  const key = JSON.stringify(data)
  useEffect(() => {
    const c = chart.current
    if (!c) return
    for (const p of [...c.panes()].reverse()) for (const s of p.getSeries()) c.removeSeries(s)
    const grid = cssVar('--chart-grid')
    c.applyOptions({ layout: { textColor: cssVar('--chart-text') }, grid: { vertLines: { color: grid }, horzLines: { color: grid } } })

    const band = c.addSeries(HistogramSeries, { priceScaleId: 'quiet', color: cssVar('--quiet-band'), priceLineVisible: false, lastValueVisible: false, title: '' }, 0)
    band.priceScale().applyOptions({ scaleMargins: { top: 0, bottom: 0 }, visible: false })
    band.setData(data.quiet.map((t) => ({ time: toTime(t), value: 1 })))

    const ref = { lineWidth: 2 as const, lineType: LineType.WithSteps, priceLineVisible: false, lastValueVisible: true }
    const nash = c.addSeries(LineSeries, { ...ref, color: cssVar('--ref-nash'), title: 'Nash' }, 0)
    nash.setData(data.nash.map((p) => ({ time: toTime(p.t), value: p.value })))
    const joint = c.addSeries(LineSeries, { ...ref, color: cssVar('--ref-joint'), lineStyle: LineStyle.Dashed, title: 'Joint opt.' }, 0)
    joint.setData(data.joint.map((p) => ({ time: toTime(p.t), value: p.value })))
    data.params.forEach((p, i) => {
      const s = c.addSeries(LineSeries, { ...ref, lineWidth: 1, color: cssVar(SELLER_COLORS[i % SELLER_COLORS.length]!), title: p.name }, 1)
      s.setData(p.points.map((x) => ({ time: toTime(x.t), value: x.value })))
    })
    const ms: SeriesMarker<Time>[] = data.markers.map((m) => ({ time: toTime(m.t), position: 'aboveBar', shape: 'arrowDown', color: cssVar('--ref-joint'), text: m.text }))
    createSeriesMarkers(nash, ms)
    c.panes().forEach((p, i) => p.setStretchFactor(i === 0 ? 3 : 1))
    c.timeScale().fitContent()
    // `key` stands for `data`, which is rebuilt on every render.
  }, [key, theme])

  const last = rounds.at(-1)
  const label = last
    ? `Preview of ${rounds.length} rounds: Nash and joint optimum per round${data.params.length ? `, changing params ${data.params.map((p) => p.name).join(', ')}` : ''}, ${data.markers.length} event markers${data.quiet.length ? `, quiet window R${data.quiet[0]} to R${data.quiet.at(-1)}` : ''}`
    : 'No preview yet'
  return <div ref={host} className="h-[300px] w-full sm:h-[360px]" role="img" aria-label={label} />
}
