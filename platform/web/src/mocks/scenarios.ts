// Mock scenario designer API: list, detail, edit, check, freeze, reference, demand models, generators and preview.
// The linear model follows the backend's closed forms (Nash 5.75 and joint optimum 10.5 at a=10, b=1.5, d=1, c=1).
import { http, HttpResponse } from 'msw/http'
import type { Problem } from '@/api/types'
import type { CheckReport, GenerateRequest, ReferencePoint, Scenario, ScenarioDraft, ScenarioEvent, ScenarioSummary, Shock } from '@/api/scenarios'

type Check = CheckReport['checks'][number]
import type { Req, Res } from './contract'
import { fixtureDemandModels, fixtureGenerators, fixtureMockDemo } from './scenarioFixtures'

const problem = (status: number, code: Problem['code'], message: string, errors: Problem['errors'] = []) =>
  HttpResponse.json({ title: message, status, code, message, errors } satisfies Problem, { status, headers: { 'Content-Type': 'application/problem+json' } })

const linear = (a: number, b: number, d: number, c: number) => ({ a, b, d, c, c_offset: {} })
const market = (params = linear(10, 1.5, 1, 1)) => ({ demand: { id: 'linear', version: '1', params }, grid: { min: 0, max: 15, step: 0.05 } })
const at = '2026-10-21T09:00:00Z'
const stored = (s: ScenarioDraft & Pick<Scenario, 'id' | 'status' | 'hash'>, approved = s.status === 'frozen'): Scenario => ({
  version: 1,
  approved_by: approved ? 'mockgen' : null,
  frozen_at: approved ? at : null,
  created_at: at,
  updated_at: at,
  ...s,
})

const initial = (): Scenario[] => [
  stored({
    id: 'scn_cost_shock_v1',
    name: 'Cost shock at round 10',
    status: 'frozen',
    hash: 'sha256:9f2c…e1',
    description: 'A common cost shock, demand news seen by A only, and a placebo.',
    market: market(),
    n_sellers: 2,
    seller_ids: ['A', 'B'],
    horizon: { min_rounds: 30, max_rounds: 30, stop_prob: 0 },
    quiet_window: [18, 25],
    events: [
      { kind: 'param_shock', announce: 8, effect: 10, length: 5, shocks: [{ path: '/c', op: 'add', value: 0.5 }], visible: ['A', 'B'], news: 'Wholesale costs rise by 0.50 per unit from round 10.', news_source: 'human', misleading: false },
      { kind: 'param_shock', announce: 12, effect: 13, length: 3, shocks: [{ path: '/a', op: 'add', value: 1 }], visible: ['A'], news: 'Demand rises: a large buyer plans more orders.', news_source: 'ai', misleading: false },
      { kind: 'news_only', announce: 27, effect: null, length: null, shocks: [], visible: ['A', 'B'], news: 'A trade magazine profiles the market.', news_source: 'human', misleading: false },
    ],
    provenance: { generator_id: 'template', generator_version: '1', seed: 0 },
  }),
  stored({ id: 'scn_flat_v1', name: 'Flat market, no events', status: 'frozen', hash: 'sha256:11aa…3b', market: market(), n_sellers: 2, seller_ids: ['A', 'B'], horizon: { min_rounds: 30, max_rounds: 30, stop_prob: 0 }, events: [], provenance: null }),
  stored({
    id: 'scn_draft_entry',
    name: 'Entry of a third seller (draft)',
    status: 'draft',
    hash: null,
    description: '',
    market: market(),
    n_sellers: 3,
    seller_ids: ['A', 'B', 'C'],
    horizon: { min_rounds: 30, max_rounds: 40, stop_prob: 0.1 },
    quiet_window: [18, 25],
    events: [{ kind: 'param_shock', announce: 6, effect: 8, length: 4, shocks: [{ path: '/a', op: 'mul', value: 0.9 }], visible: ['A', 'B', 'C'], news: 'Demand falls: a new rival opens nearby.', news_source: 'human', misleading: false }],
    provenance: null,
  }),
  structuredClone(fixtureMockDemo),
]

let scenarios = initial()
let next = 0
export const resetMockScenarios = () => {
  scenarios = initial()
}

const summarize = (s: Scenario): ScenarioSummary => ({ id: s.id, name: s.name, status: s.status, hash: s.hash, n_sellers: s.n_sellers, n_events: s.events.length, updated_at: s.updated_at })
/** Summaries for the run-config mock, which needs a frozen scenario and its hash. */
export const mockScenarioSummaries = () => scenarios.map(summarize)

const nums = (p: Record<string, unknown>) => p as Record<string, number>

const applyShock = (v: number, s: Shock) => (s.op === 'add' ? v + s.value : s.op === 'mul' ? v * s.value : s.value)

/** Params of round t: the base params with every shock active in round t applied (from `effect` for `length` rounds). */
function paramsAt(d: Pick<ScenarioDraft, 'market' | 'events'>, t: number) {
  const p = structuredClone(d.market.demand.params) as Record<string, unknown>
  for (const e of d.events) {
    if (!e.shocks?.length || t < e.effect! || t >= e.effect! + e.length!) continue
    for (const s of e.shocks) p[s.path.slice(1)] = applyShock(Number(p[s.path.slice(1)]), s)
  }
  return p
}

// The backend's word lists for the news-direction check (simcore/scenario.py UP and DOWN).
const UP = /\b(ris\w*|rose|up|increas\w*|higher|jump\w*|surg\w*|soar\w*|climb\w*|grow\w*|boost\w*)\b/i
const DOWN = /\b(fell|fall\w*|drop\w*|down|decreas\w*|lower\w*|cut\w*|declin\w*|slump\w*|plung\w*|shrink\w*|eas\w*)\b/i
const said = (text: string) => (UP.test(text) === DOWN.test(text) ? null : UP.test(text) ? 1 : -1)
const WORD = { 1: 'a rise', '-1': 'a fall', null: 'no clear direction' } as Record<string, string>

function newsChecks(d: ScenarioDraft): Check[] {
  const base = nums(d.market.demand.params)
  const out = d.events.flatMap((e, i): Check[] => {
    if (!e.news?.trim()) return [{ id: 'news_direction', ok: false, message: `Event ${i} has no news text`, event_index: i }]
    if (!e.shocks?.length || e.misleading) return []
    const signs = new Set(e.shocks.map((s) => Math.sign(applyShock(base[s.path.slice(1)] ?? 0, s) - (base[s.path.slice(1)] ?? 0))).filter((x) => x !== 0))
    const real = signs.size === 1 ? [...signs][0]! : null
    const text = said(e.news)
    if (real !== null && text === real) return []
    const change = real === null ? 'mixed directions' : real > 0 ? 'a rise' : 'a fall'
    return [{ id: 'news_direction', ok: false, message: `Event ${i}: the shocks give ${change}, but the news text gives ${WORD[String(text)]} (set misleading: true if this is on purpose)`, event_index: i }]
  })
  return out.length ? out : [{ id: 'news_direction', ok: true, message: 'News direction agrees with every shock (or the event is marked misleading)', event_index: null }]
}

function reference(d: ScenarioDraft, rounds: number): ReferencePoint[] {
  const ids = d.seller_ids?.length === d.n_sellers ? d.seller_ids : Array.from({ length: d.n_sellers }, (_, i) => String.fromCharCode(65 + i))
  return Array.from({ length: rounds }, (_, i) => {
    const t = i + 1
    const params = paramsAt(d, t)
    const { a, b, d: x, c } = nums(params)
    const nash = (a! + b! * c!) / (2 * b! - x!)
    const joint = (a! + (b! - x!) * c!) / (2 * (b! - x!))
    const each = (v: number) => Object.fromEntries(ids.map((id) => [id, v]))
    return {
      t,
      params,
      costs: each(c!),
      nash,
      joint_optimum: joint,
      nash_by_seller: each(nash),
      joint_by_seller: each(joint),
      nash_profit: (nash - c!) * (a! - (b! - x!) * nash),
      joint_profit: (joint - c!) * (a! - (b! - x!) * joint),
    }
  })
}

/** The backend's checks. The preview runs only shock_paths, stability, news_direction and visible_ids. */
function check(d: ScenarioDraft, preview = false): CheckReport {
  const ids = d.seller_ids ?? Array.from({ length: d.n_sellers }, (_, i) => String.fromCharCode(65 + i))
  const numeric = Object.entries(d.market.demand.params).filter(([, v]) => typeof v === 'number').map(([k]) => `/${k}`)
  const badPath = d.events.findIndex((e) => (e.shocks ?? []).some((s) => !numeric.includes(s.path)))
  const rounds = reference(d, d.horizon.max_rounds)
  const unstable = rounds.find((r) => !(nums(r.params).d! < nums(r.params).b!) || r.joint_optimum! > (d.market.grid?.max ?? 15))
  const [lo, hi] = (d.quiet_window ?? [18, 25]) as [number, number]
  const touches = (e: ScenarioEvent) => [e.announce, ...(e.shocks?.length ? [e.effect!, e.effect! + e.length!] : [])].some((t) => t >= lo && t <= hi)
  const quiet = d.events.findIndex(touches)
  const unseen = d.events.findIndex((e) => e.visible.some((s) => !ids.includes(s)))
  const checks: Check[] = [
    { id: 'shock_paths', ok: badPath < 0, message: badPath < 0 ? 'Every shock path is a number field of linear' : `Event ${badPath}: a shock path is not a number field of linear`, event_index: badPath < 0 ? null : badPath },
    { id: 'stability', ok: !unstable, message: unstable ? `Round ${unstable.t}: d must be less than b, and the joint optimum must be inside the grid` : 'linear checks pass in every round; Nash and joint optimum inside the grid', event_index: null },
    ...(preview ? [] : [{ id: 'quiet_window' as const, ok: quiet < 0, message: quiet < 0 ? `No announcement and no parameter change in rounds ${lo}–${hi}` : `Event ${quiet} is announced, starts or ends in rounds ${lo}–${hi}`, event_index: quiet < 0 ? null : quiet }]),
    ...newsChecks(d),
    { id: 'visible_ids', ok: unseen < 0, message: unseen < 0 ? 'All event sellers exist' : `Event ${unseen} names a seller that does not exist`, event_index: unseen < 0 ? null : unseen },
    ...(preview ? [] : [horizonCheck(d.horizon, hi)]),
  ]
  return { ok: checks.every((c) => c.ok), checks }
}

function horizonCheck(h: ScenarioDraft['horizon'], quietEnd: number): Check {
  if (h.min_rounds > h.max_rounds) return { id: 'horizon', ok: false, message: 'min_rounds is above max_rounds', event_index: null }
  if (quietEnd >= h.min_rounds) return { id: 'horizon', ok: false, message: `Quiet window ends at ${quietEnd}, not before min_rounds ${h.min_rounds}`, event_index: null }
  return { id: 'horizon', ok: true, message: 'Horizon is valid', event_index: null }
}

/** The schema rules the backend enforces on a draft (422 before any check), including Event._shape. */
function invalidDraft(d: ScenarioDraft) {
  const errors: { path: string; message: string }[] = []
  const params = d.market.demand.params
  const absent = ['c', 'a', 'b', 'd'].filter((k) => typeof params[k] !== 'number')
  if (absent.length) errors.push({ path: 'market/demand', message: `Value error, linear params: ('${absent[0]}',): Field required` })
  d.events.forEach((e, i) => {
    const kind = e.kind ?? (e.shocks?.length ? 'param_shock' : 'news_only')
    const fail = (message: string) => errors.push({ path: `events/${i}`, message: `Value error, ${message}` })
    if (kind === 'news_only' && (e.shocks?.length || e.effect != null || e.length != null)) fail('a news-only event has no shocks, effect or length')
    else if (kind === 'param_shock' && (!e.shocks?.length || e.effect == null || e.length == null)) fail('a parameter shock needs shocks, effect and length')
    else if (kind === 'param_shock' && e.effect! < e.announce) fail('effect must not come before the announcement')
  })
  return errors.length ? problem(422, 'validation_error', 'The draft does not match the scenario schema.', errors) : null
}

const find = (id: string) => scenarios.find((s) => s.id === id)
const missing = (id: string) => problem(404, 'not_found', `Scenario ${id} not found.`)
const frozen = (id: string) => problem(409, 'scenario_frozen', `${id} is frozen. Copy it to a new draft to change it.`)

const TEMPLATE_EVENTS: Record<string, ScenarioEvent[]> = {
  cost_shock_public: [{ kind: 'param_shock', announce: 6, effect: 8, length: 5, shocks: [{ path: '/c', op: 'add', value: 0.5 }], visible: ['A', 'B'], news: 'Wholesale costs rise by 0.50 per unit from round 8 for about five rounds.', news_source: 'human', misleading: false }],
  demand_news_private: [{ kind: 'param_shock', announce: 6, effect: 8, length: 5, shocks: [{ path: '/a', op: 'add', value: 1 }], visible: ['A'], news: 'Demand rises: a large buyer plans more orders.', news_source: 'human', misleading: false }],
  placebo_news: [{ kind: 'news_only', announce: 8, effect: null, length: null, shocks: [], visible: ['A', 'B'], news: 'A trade magazine profiles the market.', news_source: 'human', misleading: false }],
  quiet: [],
}

function generate(req: GenerateRequest): ScenarioDraft | Response {
  const g = fixtureGenerators.find((x) => x.id === req.generator_id)
  if (!g) return problem(404, 'unknown_generator', `Scenario generator ${req.generator_id} is not registered.`)
  const inputs = req.inputs as { name?: string; n_sellers?: number; min_rounds?: number; max_rounds?: number; stop_prob?: number; template?: string }
  const n = inputs.n_sellers ?? 2
  const seed = req.seed ?? 0
  let events: ScenarioEvent[]
  let params = linear(10, 1.5, 1, 1)
  if (g.id === 'template') {
    const tpl = TEMPLATE_EVENTS[inputs.template ?? '']
    if (!tpl) return problem(422, 'validation_error', 'The inputs do not fit the generator.', [{ path: 'inputs/template', message: `Input should be ${Object.keys(TEMPLATE_EVENTS).join(', ')}` }])
    events = structuredClone(tpl)
  } else {
    const r = (k: number) => ((seed * 9301 + 49297 * k) % 233280) / 233280
    params = linear(Math.round((9 + 2 * r(1)) * 100) / 100, 1.5, 1, Math.round((0.9 + 0.2 * r(2)) * 100) / 100)
    events = [{ kind: 'param_shock', announce: 4 + Math.floor(4 * r(3)), effect: 9, length: 4, shocks: [{ path: '/c', op: 'add', value: Math.round((0.1 + r(4) * 0.4) * 100) / 100 }], visible: ['A', 'B'], news: 'Costs rise for a few rounds.', news_source: 'human', misleading: false }]
  }
  return {
    name: inputs.name ?? 'Generated scenario',
    description: '',
    market: market(params),
    n_sellers: n,
    seller_ids: Array.from({ length: n }, (_, i) => String.fromCharCode(65 + i)),
    horizon: { min_rounds: inputs.min_rounds ?? 30, max_rounds: inputs.max_rounds ?? 40, stop_prob: inputs.stop_prob ?? 0.1 },
    quiet_window: [18, 25],
    events,
    provenance: { generator_id: g.id, generator_version: g.version, inputs: req.inputs, seed },
  }
}

export const scenarioHandlers = [
  http.get('/api/scenarios', ({ request }) => {
    const status = new URL(request.url).searchParams.get('status')
    return HttpResponse.json(scenarios.filter((s) => !status || s.status === status).map(summarize) satisfies Res<'/scenarios', 'get'>)
  }),
  http.post<never, Req<'/scenarios', 'post'>>('/api/scenarios', async ({ request }) => {
    const d = await request.json()
    const bad = invalidDraft(d)
    if (bad) return bad
    next += 1
    const now = new Date().toISOString()
    const s: Scenario = { ...d, id: `scn_MOCKNEW${next}`, status: 'draft', hash: null, version: 1, approved_by: null, frozen_at: null, created_at: now, updated_at: now }
    scenarios = [...scenarios, s]
    return HttpResponse.json(s satisfies Res<'/scenarios', 'post', 201>, { status: 201 })
  }),
  http.post('/api/scenarios/generate', async ({ request }) => {
    const out = generate((await request.json()) as GenerateRequest)
    if (out instanceof Response) return out
    return HttpResponse.json({ draft: out, check: check(out) } satisfies Res<'/scenarios/generate', 'post'>)
  }),
  http.get<{ id: string }>('/api/scenarios/:id', ({ params }) => {
    const s = find(params.id)
    return s ? HttpResponse.json(s satisfies Res<'/scenarios/{scenario_id}', 'get'>) : missing(params.id)
  }),
  http.put<{ id: string }, Req<'/scenarios/{scenario_id}', 'put'>>('/api/scenarios/:id', async ({ params, request }) => {
    const s = find(params.id)
    if (!s) return missing(params.id)
    if (s.status === 'frozen') return frozen(params.id)
    const d = await request.json()
    const bad = invalidDraft(d)
    if (bad) return bad
    const updated: Scenario = { ...d, id: s.id, status: 'draft', hash: null, version: s.version + 1, approved_by: null, frozen_at: null, created_at: s.created_at, updated_at: new Date().toISOString() }
    scenarios = scenarios.map((x) => (x.id === s.id ? updated : x))
    return HttpResponse.json(updated satisfies Res<'/scenarios/{scenario_id}', 'put'>)
  }),
  http.delete<{ id: string }>('/api/scenarios/:id', ({ params }) => {
    const s = find(params.id)
    if (!s) return missing(params.id)
    if (s.status === 'frozen') return problem(409, 'scenario_frozen', `${params.id} is frozen and cannot be deleted.`)
    scenarios = scenarios.filter((x) => x.id !== s.id)
    return new HttpResponse(null, { status: 204 })
  }),
  http.post<{ id: string }>('/api/scenarios/:id/check', ({ params }) => {
    const s = find(params.id)
    return s ? HttpResponse.json(check(s) satisfies Res<'/scenarios/{scenario_id}/check', 'post'>) : missing(params.id)
  }),
  http.post<{ id: string }, { approved_by: string }>('/api/scenarios/:id/freeze', async ({ params, request }) => {
    const s = find(params.id)
    if (!s) return missing(params.id)
    if (s.status === 'frozen') return frozen(params.id)
    const { approved_by } = await request.json()
    const report = check(s)
    if (!report.ok)
      return problem(422, 'scenario_check_failed', 'The scenario check fails; fix the draft first.', report.checks.filter((c) => !c.ok).map((c) => ({ path: `checks/${c.id}`, message: c.message })))
    const now = new Date().toISOString()
    const done: Scenario = { ...s, status: 'frozen', hash: `sha256:mock${s.id.toLowerCase()}`, approved_by, frozen_at: now, updated_at: now }
    scenarios = scenarios.map((x) => (x.id === s.id ? done : x))
    return HttpResponse.json(done satisfies Res<'/scenarios/{scenario_id}/freeze', 'post'>)
  }),
  http.get<{ id: string }>('/api/scenarios/:id/reference', ({ params }) => {
    const s = find(params.id)
    if (!s) return missing(params.id)
    const markers = s.events.flatMap((e, i) => [
      { t: e.announce, kind: 'announce' as const, event_index: i },
      ...(e.kind !== 'news_only' && e.effect != null ? [{ t: e.effect, kind: 'effect' as const, event_index: i }] : []),
      ...(e.kind !== 'news_only' && e.effect != null ? [{ t: e.effect + e.length!, kind: 'end' as const, event_index: i }] : []),
    ])
    return HttpResponse.json({
      scenario_id: s.id,
      hash: s.hash,
      demand_model: s.market.demand.id,
      demand_version: s.market.demand.version ?? '1',
      references_available: true,
      rounds: reference(s, s.horizon.max_rounds),
      markers,
    } satisfies Res<'/scenarios/{scenario_id}/reference', 'get'>)
  }),
  http.get('/api/demand-models', () => HttpResponse.json(fixtureDemandModels)),
  http.post<{ id: string }, Record<string, number>>('/api/demand-models/:id/solve', async ({ params, request }) => {
    if (params.id !== 'linear') return problem(404, 'not_found', `Demand model ${params.id} is not registered.`)
    const { nash: n, joint_optimum: j, b = 1.5, c = 1 } = await request.json()
    if (!(n! > c && n! <= j!)) return problem(422, 'validation_error', 'The targets do not fit the solver input.', [{ path: '', message: 'Value error, need c < nash <= joint_optimum' }])
    const e = (b * (n! - c)) / (2 * j! - c - n!)
    const r = (x: number) => Math.round(x * 1e9) / 1e9
    return HttpResponse.json({ model_id: 'linear', version: '1', params: { c, c_offset: {}, a: r(e * (2 * j! - c)), b, d: r(b - e) }, nash: n!, joint_optimum: j!, problems: [] } satisfies Res<'/demand-models/{model_id}/solve', 'post'>)
  }),
  http.get('/api/scenario-generators', () => HttpResponse.json(fixtureGenerators)),
  http.post<never, Req<'/market/preview', 'post'>>('/api/market/preview', async ({ request }) => {
    const b = await request.json()
    const d: ScenarioDraft = { name: 'preview', market: b.market, n_sellers: b.n_sellers, horizon: { min_rounds: b.rounds, max_rounds: b.rounds, stop_prob: 0 }, events: b.events ?? [] }
    const bad = invalidDraft(d)
    if (bad) return bad
    return HttpResponse.json({ rounds: reference(d, b.rounds), checks: check(d, true).checks } satisfies Res<'/market/preview', 'post'>)
  }),
]
