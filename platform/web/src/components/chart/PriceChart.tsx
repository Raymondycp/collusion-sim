import { useEffect, useImperativeHandle, useRef, type CSSProperties, type Ref } from 'react'
import {
  ColorType,
  createChart,
  createSeriesMarkers,
  HistogramSeries,
  LineSeries,
  LineStyle,
  LineType,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type SeriesMarker,
  type SeriesMarkerBarPosition,
  type SeriesMarkerShape,
  type SeriesType,
  type Time,
  type UTCTimestamp,
} from 'lightweight-charts'
import { cssVar, useTheme } from '@/app/theme'
import type { Marker, RunState } from '@/run/fold'

/** Rounds go on the time axis as whole days, so the chart keeps one bar per round. */
const DAY = 86_400
const toTime = (t: number) => (t * DAY) as UTCTimestamp
const fromTime = (time: Time) => Math.round((time as number) / DAY)
const roundLabel = (time: Time) => `R${fromTime(time)}`

export const SELLER_COLORS = ['--series-1', '--series-2', '--series-3', '--series-4']
export const sellerColor = (sellers: string[], id: string) => cssVar(SELLER_COLORS[Math.max(0, sellers.indexOf(id)) % SELLER_COLORS.length]!)
/** A market has up to six sellers and there are four colours, so sellers 5 and 6 repeat a colour with a dashed line. */
const sellerLineStyle = (sellers: string[], id: string) => (sellers.indexOf(id) >= SELLER_COLORS.length ? LineStyle.Dashed : LineStyle.Solid)

export type Overlay = { id: string; label: string; state: RunState }

/** An indicator series as the API returned it, already cut at the replay cursor. */
export type ChartIndicator = {
  key: string
  label: string
  pane: 'overlay' | 'separate'
  outputs: { name: string; kind: 'line' | 'histogram'; points: { t: number; value: number }[] }[]
}

// Separate from the seller colours, so an overlay indicator never reads as a seller's price.
const INDICATOR_COLORS = ['--ind-1', '--ind-2', '--ind-3']
const PANE_PX = 110
export type PriceChartHandle = { screenshot: () => HTMLCanvasElement | null }

type Props = {
  state: RunState
  overlays: Overlay[]
  indicators?: ChartIndicator[]
  onPickT?: (t: number) => void
  ref?: Ref<PriceChartHandle>
}

const MARKER_STYLE: Record<Marker['kind'], { shape: SeriesMarkerShape; position: SeriesMarkerBarPosition; text: string; color?: string }> = {
  news: { shape: 'circle', position: 'aboveBar', text: 'N', color: '--ref-nash' },
  // No letter: the square has the seller's line colour, and a letter on every message repeats A and B in each round.
  post: { shape: 'square', position: 'aboveBar', text: '' },
  probe: { shape: 'arrowDown', position: 'aboveBar', text: 'Probe', color: '--ref-joint' },
  placebo: { shape: 'arrowDown', position: 'aboveBar', text: 'Placebo', color: '--ref-nash' },
  ablation: { shape: 'arrowDown', position: 'aboveBar', text: 'Ablation', color: '--ref-joint' },
  fork: { shape: 'arrowDown', position: 'aboveBar', text: 'Fork', color: '--ref-nash' },
  fallback: { shape: 'arrowUp', position: 'belowBar', text: 'F', color: '--ref-joint' },
  budget: { shape: 'circle', position: 'belowBar', text: '$', color: '--ref-joint' },
}

export function PriceChart({ state, overlays, indicators = [], onPickT, ref }: Props) {
  const host = useRef<HTMLDivElement>(null)
  const chart = useRef<IChartApi | null>(null)
  const series = useRef(new Map<string, ISeriesApi<SeriesType>>())
  const markers = useRef<ISeriesMarkersPluginApi<Time> | null>(null)
  const fittedTo = useRef('')
  const pick = useRef(onPickT)
  useEffect(() => {
    pick.current = onPickT
  }, [onPickT])
  const theme = useTheme()

  useImperativeHandle(ref, () => ({ screenshot: () => chart.current?.takeScreenshot() ?? null }), [])

  useEffect(() => {
    const c = createChart(host.current!, {
      autoSize: true,
      layout: { attributionLogo: true, background: { type: ColorType.Solid, color: 'transparent' }, fontFamily: 'Geist Variable, sans-serif' },
      localization: { timeFormatter: roundLabel, priceFormatter: (p: number) => p.toFixed(2) },
      timeScale: { tickMarkFormatter: roundLabel, fixLeftEdge: true, rightOffset: 2 },
      rightPriceScale: { scaleMargins: { top: 0.15, bottom: 0.1 } },
      crosshair: { mode: 0 },
    })
    c.subscribeClick((p) => {
      if (p.time !== undefined) pick.current?.(fromTime(p.time))
    })
    chart.current = c
    const s = series.current
    return () => {
      c.remove()
      chart.current = null
      s.clear()
      markers.current = null
      // A new chart (StrictMode remount, or a remount with the same data) must fit again.
      fittedTo.current = ''
    }
  }, [])

  useEffect(() => {
    const c = chart.current
    if (!c) return
    const text = cssVar('--chart-text')
    const grid = cssVar('--chart-grid')
    c.applyOptions({
      layout: { textColor: text },
      grid: { vertLines: { color: grid }, horzLines: { color: grid } },
    })

    const wanted = new Set<string>()
    const upsert = (key: string, kind: 'line' | 'histogram', pane: number, opts: Record<string, unknown>) => {
      wanted.add(key)
      let s = series.current.get(key)
      if (s && s.seriesType() !== (kind === 'line' ? 'Line' : 'Histogram')) {
        c.removeSeries(s)
        s = undefined
      }
      if (!s) {
        const base = { priceLineVisible: false, lastValueVisible: false, ...opts }
        s = kind === 'line' ? c.addSeries(LineSeries, base, pane) : c.addSeries(HistogramSeries, base, pane)
        series.current.set(key, s)
      } else {
        s.applyOptions(opts)
        if (s.getPane().paneIndex() !== pane) s.moveToPane(pane)
      }
      return s
    }
    const line = (key: string, pane: number, opts: Record<string, unknown>) => upsert(key, 'line', pane, opts) as ISeriesApi<'Line'>

    const sellers = state.sellers
    const nash = line('ref:nash', 0, { color: cssVar('--ref-nash'), lineWidth: 1, lineStyle: LineStyle.Dashed, lineType: LineType.WithSteps, title: 'Nash' })
    nash.setData(state.reference.flatMap((r) => (r.nash === null ? [] : [{ time: toTime(r.t), value: r.nash }])))
    const joint = line('ref:joint', 0, { color: cssVar('--ref-joint'), lineWidth: 1, lineStyle: LineStyle.Dashed, lineType: LineType.WithSteps, title: 'Joint opt.' })
    joint.setData(state.reference.flatMap((r) => (r.joint_optimum === null ? [] : [{ time: toTime(r.t), value: r.joint_optimum }])))

    let first: ISeriesApi<'Line'> | null = null
    for (const id of sellers) {
      const s = line(`price:${id}`, 0, { color: sellerColor(sellers, id), lineStyle: sellerLineStyle(sellers, id), lineWidth: 2, title: id, lastValueVisible: true })
      s.setData((state.prices[id] ?? []).map((p) => ({ time: toTime(p.t), value: p.value })))
      first ??= s
    }

    overlays.forEach((o, i) => {
      for (const id of o.state.sellers) {
        const s = line(`overlay:${o.id}:${id}`, 0, {
          color: sellerColor(sellers, id),
          lineWidth: 2,
          lineStyle: i % 2 === 0 ? LineStyle.Dotted : LineStyle.SparseDotted,
          title: `${id} ${o.label}`,
        })
        s.setData((o.state.prices[id] ?? []).map((p) => ({ time: toTime(p.t), value: p.value })))
      }
    })

    const cs = line('surplus', 1, { color: cssVar('--series-3'), lineWidth: 1, title: 'Consumer surplus', lastValueVisible: true })
    cs.setData(state.surplus.map((p) => ({ time: toTime(p.t), value: p.value })))

    let nextPane = 2
    indicators.forEach((ind, i) => {
      const pane = ind.pane === 'overlay' ? 0 : nextPane++
      ind.outputs.forEach((o, j) => {
        const color = cssVar(INDICATOR_COLORS[(i + j) % INDICATOR_COLORS.length]!)
        const s = upsert(`ind:${ind.key}:${o.name}`, o.kind, pane, {
          color,
          title: ind.outputs.length > 1 ? `${ind.label} ${o.name}` : ind.label,
          lastValueVisible: true,
          ...(o.kind === 'line' ? { lineWidth: 2, lineStyle: ind.pane === 'overlay' ? LineStyle.LargeDashed : LineStyle.Solid } : {}),
        })
        s.setData(o.points.map((p) => ({ time: toTime(p.t), value: p.value })))
      })
    })

    for (const [key, s] of series.current) {
      if (!wanted.has(key)) {
        c.removeSeries(s)
        series.current.delete(key)
      }
    }
    const panes = c.panes()
    panes.forEach((p, i) => p.setStretchFactor(i === 0 ? 4 : 1))

    const allMarkers = [...state.markers, ...overlays.flatMap((o) => o.state.markers.filter((m) => m.kind !== 'post'))]
    const ms: SeriesMarker<Time>[] = allMarkers
      .sort((a, b) => a.t - b.t)
      .map((m) => {
        const st = MARKER_STYLE[m.kind]
        const color = st.color ? cssVar(st.color) : sellerColor(sellers, m.agentId ?? '')
        return { time: toTime(m.t), shape: st.shape, position: st.position, color, text: st.text, size: 1 }
      })
    const span = `${state.reference[0]?.t}-${state.reference[state.reference.length - 1]?.t}-${overlays.length}`
    if (span !== fittedTo.current) {
      fittedTo.current = span
      c.timeScale().fitContent()
    }

    if (first) {
      if (!markers.current) markers.current = createSeriesMarkers(first, ms)
      else markers.current.setMarkers(ms)
    }
  }, [state, overlays, indicators, theme])

  const extra = indicators.filter((i) => i.pane === 'separate').length
  const rounds = state.reference.length ? `rounds R${state.reference[0]!.t} to R${state.reference[state.reference.length - 1]!.t}` : 'no rounds yet'
  const label = [
    `Price chart for ${state.sellers.join(', ') || 'no sellers'}, ${rounds}`,
    overlays.length ? `${overlays.length} branch overlay${overlays.length > 1 ? 's' : ''}` : null,
    indicators.length ? `indicators: ${indicators.map((i) => i.label).join(', ')}` : null,
  ]
    .filter(Boolean)
    .join('; ')
  return (
    <div
      ref={host}
      className="h-[calc(340px+var(--extra))] w-full sm:h-[calc(420px+var(--extra))]"
      style={{ '--extra': `${extra * PANE_PX}px` } as CSSProperties}
      role="img"
      aria-label={label}
    />
  )
}
