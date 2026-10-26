import type { DemandModel, Scenario, ScenarioDraft, ScenarioEvent, ScenarioMarker } from '@/api/scenarios'
import { defaultFor, flatten } from '@/lib/jsonSchema'

const DRAFT_KEYS = ['name', 'description', 'market', 'n_sellers', 'seller_ids', 'horizon', 'quiet_window', 'events', 'provenance'] as const

/** The editable part of a stored scenario: what PUT and POST accept. */
export function toDraft(s: Scenario | ScenarioDraft): ScenarioDraft {
  const out: Record<string, unknown> = {}
  for (const k of DRAFT_KEYS) if (s[k] !== undefined) out[k] = structuredClone(s[k])
  return out as ScenarioDraft
}

export const sellerIds = (d: Pick<ScenarioDraft, 'n_sellers' | 'seller_ids'>) =>
  d.seller_ids?.length === d.n_sellers ? d.seller_ids : Array.from({ length: d.n_sellers }, (_, i) => String.fromCharCode(65 + i))

/** Keeps the ids that are still in range, and adds A, B, C… ids that are not taken yet. */
export function resizeSellers(ids: string[], n: number) {
  const kept = ids.slice(0, n)
  for (let c = 65; kept.length < n; c++) if (!kept.includes(String.fromCharCode(c))) kept.push(String.fromCharCode(c))
  return kept
}

/** The backend's default. Every scenario has a quiet window, so the editor never drops it. */
export const QUIET_WINDOW = [18, 25]

/** A draft with the model's default params and no events. The person picks the model; nothing is chosen for them. */
export function starterDraft(m: DemandModel): ScenarioDraft {
  return {
    name: '',
    description: '',
    market: { demand: { id: m.id, version: m.version, params: defaultFor(m.params_schema, m.params_schema) as Record<string, unknown> }, grid: { min: 0, max: 15, step: 0.05 } },
    n_sellers: 2,
    seller_ids: ['A', 'B'],
    horizon: { min_rounds: 30, max_rounds: 30, stop_prob: 0 },
    quiet_window: [...QUIET_WINDOW],
    events: [],
  }
}

/** A parameter shock as the backend needs it: shocks, effect and length. It starts with one shock on the first number param. */
export const newEvent = (sellers: string[], announce: number, paths: string[]): ScenarioEvent => ({
  kind: 'param_shock',
  announce,
  effect: announce,
  length: 3,
  shocks: paths[0] ? [{ path: paths[0], op: 'add', value: 0 }] : [],
  visible: [...sellers],
  news: '',
  news_source: 'human',
  misleading: false,
})

/** The same markers as GET /scenarios/{id}/reference: `end` is the first round after the shock. */
export function markersFor(events: ScenarioEvent[]): ScenarioMarker[] {
  return events.flatMap((e, i) => {
    const out: ScenarioMarker[] = [{ t: e.announce, kind: 'announce', event_index: i }]
    if (e.kind !== 'news_only' && e.effect != null) {
      out.push({ t: e.effect, kind: 'effect', event_index: i })
      if (e.length != null) out.push({ t: e.effect + e.length, kind: 'end', event_index: i })
    }
    return out
  })
}

export type Change = { path: string; before: unknown; after: unknown }

/** Leaf-by-leaf difference, so the Generate dialog can show what a generated draft changes. */
export function draftDiff(before: ScenarioDraft | null, after: ScenarioDraft): Change[] {
  const a = before ? flatten(toDraft(before)) : {}
  const b = flatten(toDraft(after))
  return [...new Set([...Object.keys(a), ...Object.keys(b)])]
    .filter((k) => JSON.stringify(a[k]) !== JSON.stringify(b[k]))
    .sort()
    .map((k) => ({ path: k, before: a[k], after: b[k] }))
}

/** Numeric params that a shock may target, as JSON pointers (`/c`). */
export const shockPaths = (m: DemandModel | undefined) =>
  Object.entries(m?.params_schema.properties ?? {})
    .filter(([, s]) => s.type === 'number' || s.type === 'integer')
    .map(([k]) => `/${k}`)
