// Mock indicator server. These formulas stand in for de's indicators/ package so the UI can be built.
// The real definitions live in the backend; the browser never computes indicators.
import type { IndicatorInstance, IndicatorParams, IndicatorSpec } from '@/api/indicators'
import type { SimEvent } from '@/api/types'

export const mockIndicatorSpecs: IndicatorSpec[] = [
  {
    id: 'delta_p',
    version: 'mock-1',
    name: 'Δp (normalised price index)',
    description: 'Mean price minus Nash, divided by joint optimum minus Nash. 0 = Nash, 1 = joint optimum.',
    data_layer: 'B',
    pane: 'separate',
    inputs: ['price.*', 'nash.*', 'joint_optimum.*'],
    params_schema: { type: 'object', properties: { smooth: { type: 'integer', title: 'Smoothing window (rounds)', minimum: 1, maximum: 10, default: 1 } } },
    outputs: [{ name: 'delta_p', kind: 'line', unit: 'ratio' }],
  },
  {
    id: 'br_gap_net',
    version: 'mock-1',
    name: 'Net best-response gap',
    description: 'Mean of price minus best response to the rivals, over sellers.',
    data_layer: 'B',
    pane: 'separate',
    inputs: ['price.*', 'best_response.*'],
    params_schema: { type: 'object', properties: {} },
    outputs: [{ name: 'br_gap', kind: 'line', unit: 'price' }],
  },
  {
    id: 'corr_raw',
    version: 'mock-1',
    name: 'Price-change correlation (rolling)',
    description: 'Correlation of the price changes of the first two sellers over a trailing window.',
    data_layer: 'A',
    pane: 'separate',
    inputs: ['price.*'],
    params_schema: { type: 'object', properties: { window: { type: 'integer', title: 'Window (rounds)', minimum: 3, maximum: 20, default: 8 } } },
    outputs: [{ name: 'corr', kind: 'line', unit: 'ratio' }],
  },
  {
    id: 'msg_volume',
    version: 'mock-1',
    name: 'Message volume',
    description: 'Messages posted per round, all channels.',
    data_layer: 'A',
    pane: 'separate',
    inputs: ['posts'],
    params_schema: { type: 'object', properties: {} },
    outputs: [{ name: 'messages', kind: 'histogram', unit: 'count' }],
  },
  {
    id: 'mean_price_ma',
    version: 'mock-1',
    name: 'Mean price, moving average',
    description: 'Trailing average of the mean seller price.',
    data_layer: 'A',
    pane: 'overlay',
    inputs: ['price.*'],
    params_schema: { type: 'object', properties: { window: { type: 'integer', title: 'Window (rounds)', minimum: 2, maximum: 20, default: 5 } } },
    outputs: [{ name: 'ma', kind: 'line', unit: 'price' }],
  },
]

type Row = { t: number; prices: number[]; best_response: number[]; nash: number | null; joint_optimum: number | null; posts: number }

function rows(events: SimEvent[], until: number): Row[] {
  const posts = new Map<number, number>()
  const prices = new Map<number, number[]>()
  const out: Row[] = []
  for (const e of events) {
    if (e.t > until) break
    if (e.type === 'posted') posts.set(e.t, (posts.get(e.t) ?? 0) + 1)
    if (e.type === 'settled') prices.set(e.t, Object.values(e.prices))
    // `reference` comes right after `settled` at the same t (contract v0.6.0).
    if (e.type === 'reference' && prices.has(e.t)) {
      out.push({ t: e.t, prices: prices.get(e.t)!, best_response: Object.values(e.best_response), nash: e.reference.nash ?? null, joint_optimum: e.reference.joint_optimum ?? null, posts: 0 })
    }
  }
  for (const r of out) r.posts = posts.get(r.t) ?? 0
  return out
}

const mean = (xs: number[]) => xs.reduce((a, b) => a + b, 0) / xs.length
const trailing = (xs: (number | null)[], w: number) =>
  xs.map((_, i) => {
    const win = xs.slice(Math.max(0, i - w + 1), i + 1).filter((x): x is number => x !== null)
    return win.length === Math.min(w, i + 1) ? mean(win) : null
  })

function corr(a: number[], b: number[]) {
  const ma = mean(a)
  const mb = mean(b)
  let num = 0
  let da = 0
  let db = 0
  for (let i = 0; i < a.length; i++) {
    num += (a[i]! - ma) * (b[i]! - mb)
    da += (a[i]! - ma) ** 2
    db += (b[i]! - mb) ** 2
  }
  return da === 0 || db === 0 ? null : num / Math.sqrt(da * db)
}

const r3 = (x: number | null) => (x === null ? null : Math.round(x * 1000) / 1000)

/** Rows up to `until`; every indicator below reads only rows <= its own round. Returns null for an unknown id. */
export function computeMockIndicator(id: string, events: SimEvent[], given: IndicatorParams, until: number): Omit<IndicatorInstance, 'run_id'> | null {
  return computeOne(id, rows(events, until), given)
}

function computeOne(id: string, rs: Row[], given: IndicatorParams): Omit<IndicatorInstance, 'run_id'> | null {
  const spec = mockIndicatorSpecs.find((s) => s.id === id)
  if (!spec) return null
  const params = { ...Object.fromEntries(Object.entries((spec.params_schema as { properties: Record<string, { default?: number }> }).properties).map(([k, v]) => [k, v.default ?? 0])), ...given }
  let outputs: Record<string, (number | null)[]>
  switch (id) {
    case 'delta_p': {
      const raw = rs.map((r) => (r.nash === null || r.joint_optimum === null ? null : (mean(r.prices) - r.nash) / (r.joint_optimum - r.nash)))
      outputs = { delta_p: trailing(raw, Number(params.smooth ?? 1)).map(r3) }
      break
    }
    case 'br_gap_net':
      outputs = { br_gap: rs.map((r) => r3(mean(r.prices.map((p, i) => p - r.best_response[i]!)))) }
      break
    case 'corr_raw': {
      const w = Number(params.window ?? 8)
      const dA = rs.map((r, i) => (i === 0 ? 0 : r.prices[0]! - rs[i - 1]!.prices[0]!))
      const dB = rs.map((r, i) => (i === 0 ? 0 : r.prices[1]! - rs[i - 1]!.prices[1]!))
      outputs = { corr: rs.map((_, i) => (i + 1 < w ? null : r3(corr(dA.slice(i - w + 1, i + 1), dB.slice(i - w + 1, i + 1))))) }
      break
    }
    case 'msg_volume':
      outputs = { messages: rs.map((r) => r.posts) }
      break
    case 'mean_price_ma':
      outputs = { ma: trailing(rs.map((r) => mean(r.prices)), Number(params.window ?? 5)).map(r3) }
      break
    default:
      return null
  }
  return { indicator_id: id, version: spec.version, params, until: rs[rs.length - 1]?.t ?? 0, t: rs.map((r) => r.t), outputs }
}
