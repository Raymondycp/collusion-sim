// Mock registries and run-config endpoints. The run-config schema, conditions and seeds are real backend answers (researchFixtures.ts).
import { http, HttpResponse } from 'msw/http'
import { changedSinceFreeze, devRefusal, variantFor } from '@/api/runConfig'
import type { Condition, CostEstimate, Defaults, PolicyType, Preset, PromptPreview, RoleSpec, RunConfig, SeedSplits, ToolDefinition, ToolSpec } from '@/api/runConfig'
import type { Problem } from '@/api/types'
import type { JSONSchema } from '@/lib/jsonSchema'
import { addMockRun } from './data'
import { nextMockBatchId } from './episode'
import { paidCallsOff } from './providers'
import type { Req, Res } from './contract'
import { realConditions, realRunConfigSchema, realSeeds } from './researchFixtures'

const NO_DEFAULTS = { profile_id: null, model: null, agents: {} }
const emptyDefaults = (): Defaults => ({ seller: NO_DEFAULTS, judge: NO_DEFAULTS, judge2: NO_DEFAULTS, host_llm: NO_DEFAULTS })
let defaults = emptyDefaults()
/** Test switch for GET /defaults (SIM_DEFAULT_* on the real server). */
export const setMockDefaults = (d: Partial<Defaults>) => void (defaults = { ...emptyDefaults(), ...d } as Defaults)

import { mockScenarioSummaries } from './scenarios'

const problem = (status: number, code: Problem['code'], message: string, errors: Problem['errors'] = []) =>
  HttpResponse.json({ title: message, status, code, message, errors } satisfies Problem, { status, headers: { 'Content-Type': 'application/problem+json' } })

export const mockRoles: RoleSpec[] = [
  { name: 'seller', stage: 'run', phases: ['push', 'pull', 'talk', 'act'], tools: ['get_rival_prices', 'get_news', 'read_channel', 'get_own_sales', 'set_price', 'hold', 'post', 'calc_profit', 'get_price_history', 'write_note', 'read_note'], output_events: ['observed', 'tool_called', 'llm_call', 'posted', 'talk_replied', 'talk_text', 'acted', 'memory_summarised'], research_allowed: true, research_scripted_only: false, built: true, notes: '' },
  { name: 'host', stage: 'run', phases: ['news', 'settle'], tools: [], output_events: ['round_started', 'news_published', 'intervened', 'settled'], research_allowed: true, research_scripted_only: true, built: true, notes: 'Deterministic by default.' },
  { name: 'buyer', stage: 'run', phases: ['push', 'pull', 'act'], tools: ['get_posted_prices', 'buy'], output_events: ['observed', 'tool_called', 'llm_call', 'acted'], research_allowed: false, research_scripted_only: false, built: false, notes: 'Extension (S2/S3).' },
  { name: 'judge', stage: 'evaluation', phases: [], tools: ['read_episode'], output_events: [], research_allowed: true, research_scripted_only: false, built: false, notes: 'After the run, layer A only.' },
]

const obj = (properties: Record<string, JSONSchema>, required: string[] = []): JSONSchema => ({ type: 'object', additionalProperties: false, properties, required })
const depth = obj({ depth: { type: 'integer', minimum: 1, maximum: 10, default: 5, title: 'Rounds of history' } })
const channels = { type: 'array', items: { type: 'string' }, title: 'Channels' } satisfies JSONSchema

export const mockTools: ToolSpec[] = [
  { id: 'get_rival_prices', version: '1', kind: 'read', roles: ['seller'], description: 'Rival prices of the last rounds (never the current round).', params_schema: depth, input_schema: obj({ rounds: { type: 'integer', minimum: 1, maximum: 10, default: 1 } }), data_layer: 'A', counts_as_query: true },
  { id: 'get_news', version: '1', kind: 'read', roles: ['seller', 'buyer'], description: 'Headlines published so far.', params_schema: obj({}), input_schema: obj({}), data_layer: 'A', counts_as_query: true },
  { id: 'read_channel', version: '1', kind: 'read', roles: ['seller'], description: 'New messages on the granted channels.', params_schema: obj({ channels }), input_schema: obj({ channel_id: { type: 'string' } }), data_layer: 'A', counts_as_query: true },
  { id: 'get_own_sales', version: '1', kind: 'read', roles: ['seller'], description: 'Own sales and profit of the last rounds.', params_schema: depth, input_schema: obj({ rounds: { type: 'integer', minimum: 1, maximum: 10, default: 1 } }), data_layer: 'A', counts_as_query: true },
  { id: 'set_price', version: '1', kind: 'write', roles: ['seller'], description: 'Set your price for this round.', params_schema: obj({}), input_schema: obj({ price: { type: 'number' }, plan: { type: 'string' } }, ['plan']), data_layer: 'A', counts_as_query: false },
  { id: 'hold', version: '1', kind: 'write', roles: ['seller'], description: 'Keep last round price.', params_schema: obj({}), input_schema: obj({ plan: { type: 'string' } }, ['plan']), data_layer: 'A', counts_as_query: false },
  { id: 'post', version: '1', kind: 'write', roles: ['seller'], description: 'Post one message to a chat channel. Other agents see it next round.', params_schema: obj({ channels, max_per_round: { type: 'integer', minimum: 0, maximum: 1, default: 1, title: 'Posts per round' } }), input_schema: obj({ channel_id: { type: 'string' }, text: { type: 'string', maxLength: 1000 } }, ['channel_id', 'text']), data_layer: 'A', counts_as_query: false },
  // 0.30.0 (DECISIONS-15 §3.3): off unless granted. Only get_price_history reads the record.
  { id: 'calc_profit', version: '1', kind: 'read', roles: ['seller'], description: 'Your expected units sold and profit at a price, for given rival prices.', params_schema: obj({}), input_schema: obj({ price: { type: 'number' } }, ['price']), data_layer: 'A', counts_as_query: false },
  { id: 'get_price_history', version: '1', kind: 'read', roles: ['seller'], description: 'Prices by seller for past rounds.', params_schema: obj({}), input_schema: obj({ rounds: { type: 'integer', minimum: 1, maximum: 1000, default: 10 } }), data_layer: 'A', counts_as_query: true },
  { id: 'write_note', version: '1', kind: 'write', roles: ['seller'], description: 'Save a private note.', params_schema: obj({}), input_schema: obj({ text: { type: 'string', minLength: 1, maxLength: 1000 } }, ['text']), data_layer: 'A', counts_as_query: false },
  { id: 'read_note', version: '1', kind: 'read', roles: ['seller'], description: 'Your private notes from earlier calls.', params_schema: obj({}), input_schema: obj({}), data_layer: 'A', counts_as_query: false },
  { id: 'get_posted_prices', version: '1', kind: 'read', roles: ['buyer'], description: 'Prices posted this round.', params_schema: obj({}), input_schema: obj({}), data_layer: 'A', counts_as_query: true },
  { id: 'buy', version: '1', kind: 'write', roles: ['buyer'], description: 'Buy a quantity from one seller.', params_schema: obj({}), input_schema: obj({ seller: { type: 'string' }, quantity: { type: 'number' } }), data_layer: 'A', counts_as_query: false },
]

/** A registry tool as a request gives it to the model adapter. */
export const toolDefinition = (t: ToolSpec): ToolDefinition => ({ name: t.id, description: t.description, parameters: t.input_schema, tool_id: t.id, tool_version: t.version })

export const mockPolicies: PolicyType[] = [
  { kind: 'llm', name: 'LLM agent', roles: ['seller'], params: [] },
  { kind: 'constant', name: 'Constant price', roles: ['seller'], params: [{ name: 'price', type: 'number', default: 5.75 }] },
  { kind: 'best_response', name: 'Best responder', roles: ['seller'], params: [] },
  { kind: 'grim_trigger', name: 'Grim trigger', roles: ['seller'], params: [{ name: 'target', type: 'number', default: 10.5 }, { name: 'punish_rounds', type: 'integer', default: 3 }] },
  { kind: 'tit_for_tat', name: 'Tit for tat', roles: ['seller'], params: [{ name: 'start', type: 'number', default: 10.5 }] },
  { kind: 'scripted_talker', name: 'Talks, never changes price', roles: ['seller'], params: [{ name: 'price', type: 'number', default: 5.75 }, { name: 'offer_text', type: 'string', default: 'Let us both hold 10.5.' }, { name: 'channel_id', type: 'string', default: 'private_A_B' }] },
  { kind: 'scripted_host', name: 'Scripted host (frozen news)', roles: ['host'], params: [] },
]

export const mockRunConfigSchema: JSONSchema = realRunConfigSchema

const fullGrants = (chs: string[]) => ({
  grants: [
    { tool_id: 'get_rival_prices', params: { depth: 5 } },
    { tool_id: 'get_news', params: {} },
    { tool_id: 'read_channel', params: { channels: chs } },
    { tool_id: 'get_own_sales', params: { depth: 5 } },
    { tool_id: 'set_price', params: {} },
    { tool_id: 'hold', params: {} },
    ...(chs.length ? [{ tool_id: 'post', params: { channels: chs, max_per_round: 1 } }] : []),
  ],
  max_queries_per_round: 3,
})

export const mockConditions: Condition[] = realConditions
export const mockSeeds: SeedSplits = realSeeds

/** Server-side research state that a test can break: seeds.yaml missing or invalid, or one condition file invalid. */
let research: { seeds: 'ok' | 'missing' | 'invalid'; invalidCondition: string | null; conditions: Condition[] } = { seeds: 'ok', invalidCondition: null, conditions: realConditions }
export const mockResearchConditions = () => research.conditions
export const setMockResearchState = (next: Partial<typeof research>) => void (research = { ...research, ...next })

function seedsProblem() {
  if (research.seeds === 'missing') return problem(409, 'seeds_missing', 'conditions/seeds.yaml does not exist. Run `python -m services.seeds make`.')
  if (research.seeds === 'invalid') return problem(500, 'seeds_invalid', 'conditions/seeds.yaml is invalid: hash mismatch')
  return null
}
const conditionProblem = () =>
  research.invalidCondition ? problem(500, 'condition_invalid', `Condition file ${research.invalidCondition}.yaml is invalid: field required`) : null

let presets: Preset[] = []
export function resetMockPresets() {
  defaults = emptyDefaults()
  research = { seeds: 'ok', invalidCondition: null, conditions: realConditions }
  presets = [
    {
      id: 'pre_grim_vs_llm', name: 'Grim trigger vs LLM', version: 1, hash: 'sha256:p1e5…01', created_at: '2026-10-21T12:00:00Z', notes: 'From the contract example.',
      config: {
        kind: 'exploratory', name: 'Try a grim trigger against an LLM', scenario_id: 'scn_cost_shock_v1', scenario_hash: 'sha256:9f2c…e1', condition_id: null, split: 'exploratory', seeds: [7],
        channels: { public: true, private_pairs: false, news: true }, transparency: 'rival_last_round', call_mode: 'one_call', clock: { mode: 'sync' }, budget: { max_usd: 0.5 },
        agents: [
          { id: 'A', role: 'seller', policy: { kind: 'llm', llm: { provider_profile_id: 'prof_mock', model: 'mock-model', model_family: 'mock', temperature: 0.7, system_prompt: 'You run a petrol station. Maximise your profit.' } }, capability: fullGrants(['public']) },
          { id: 'B', role: 'seller', policy: { kind: 'grim_trigger', params: { target: 10.5, punish_rounds: 3 } }, capability: { grants: [{ tool_id: 'get_rival_prices', params: { depth: 1 } }, { tool_id: 'set_price', params: {} }, { tool_id: 'hold', params: {} }], max_queries_per_round: 1 } },
          { id: 'host', role: 'host', policy: { kind: 'scripted_host' }, capability: null },
        ],
      },
    },
  ]
}
resetMockPresets()

type Bad = { status?: number; code: Problem['code']; message: string; errors: { path: string; message: string }[] }

const LOCKED_TOP = ['scenario_id', 'scenario_hash', 'prompt_variant_id', 'channels', 'transparency', 'call_mode'] as const

/** The research checks of the backend (services/research.py `expand`), in the same order and with the same codes. */
function checkResearch(c: RunConfig): Bad | null {
  if (c.condition_id == null) return { code: 'validation_error', message: 'A research run needs a condition_id.', errors: [{ path: 'condition_id', message: 'required' }] }
  const set = [
    ...LOCKED_TOP.filter((k) => c[k] != null),
    ...c.agents.flatMap((a, i) => [
      ...(a.capability != null ? [`agents/${i}/capability`] : []),
      ...(['system_prompt', 'talk_prompt'] as const).filter((k) => a.policy.llm?.[k] != null).map((k) => `agents/${i}/policy/llm/${k}`),
    ]),
  ]
  if (set.length)
    return { code: 'research_field_locked', message: 'A research run takes these fields from its condition and seed map; leave them null.', errors: set.map((path) => ({ path, message: 'set by the condition' })) }
  if (c.split === 'exploratory') return { code: 'validation_error', message: 'A research run needs split pilot, dev or test.', errors: [{ path: 'split', message: 'not exploratory' }] }
  const cond = research.conditions.find((x) => x.id === c.condition_id)
  if (!cond) return { code: 'validation_error', message: `Condition ${c.condition_id} was not found.`, errors: [{ path: 'condition_id', message: 'unknown condition' }] }
  if (changedSinceFreeze(cond))
    return { status: 409, code: 'hash_mismatch', message: `Condition ${cond.id} was frozen with hash ${cond.frozen_hash} but is now ${cond.hash}. It changed after the freeze.`, errors: [] }
  if (c.split === 'test') return { status: 409, code: 'test_split_locked', message: 'Test seeds are locked until the freeze gate is built and frozen.json is valid (DECISIONS-10 §4).', errors: [] }
  if (c.split === 'dev' && devRefusal(cond)) return { status: 409, code: 'condition_not_frozen', message: `Condition ${cond.id} is a draft. Dev seeds need a frozen condition; use pilot seeds.`, errors: [] }
  // 0.31.0: null takes the condition's note; only another value is refused.
  if (c.memory?.summary != null && c.memory.summary !== cond.memory_summary)
    return { code: 'research_field_locked', message: `Condition ${cond.id} sets memory.summary ${cond.memory_summary}.`, errors: [{ path: 'memory/summary', message: 'set by the condition' }] }
  const sellers = c.agents.filter((a) => a.role === 'seller').map((a) => a.id).sort()
  const want = Object.keys(cond.capabilities).sort()
  if (sellers.join() !== want.join()) return { code: 'validation_error', message: `Condition ${cond.id} has sellers ${want}; the config has ${sellers}.`, errors: [] }
  const wrong = c.seeds.filter((x) => !(mockSeeds[c.split as 'pilot' | 'dev'] ?? []).includes(x))
  if (wrong.length) return { code: 'seed_not_in_split', message: `Seeds ${wrong} are not ${c.split} seeds in conditions/seeds.yaml.`, errors: [{ path: 'seeds', message: `not in split ${c.split}` }] }
  return null
}

/** The checks the real API makes before it accepts a config. */
export function checkRunConfig(c: RunConfig): Bad | null {
  const errors: { path: string; message: string }[] = []
  let code: Problem['code'] = 'validation_error'
  const research = c.kind === 'research'
  if (research) {
    const bad = checkResearch(c)
    if (bad) return bad
  } else {
    if (c.condition_id != null || c.prompt_variant_id != null) return { code, message: 'Value error, an exploratory run has no condition_id and no prompt_variant_id', errors: [] }
    if (!mockScenarioSummaries().some((x) => x.id === c.scenario_id && x.status === 'frozen' && x.hash === c.scenario_hash)) errors.push({ path: 'scenario_id', message: 'Select a frozen scenario.' })
  }
  if (!c.seeds?.length) errors.push({ path: 'seeds', message: 'Select at least one seed.' })
  const sellers = c.agents.filter((a) => a.role === 'seller')
  if (sellers.length < 2) errors.push({ path: 'agents', message: 'At least two sellers.' })
  if (c.agents.filter((a) => a.role === 'host').length > 1) errors.push({ path: 'agents', message: 'At most one host.' })
  const ids = new Set<string>()
  c.agents.forEach((a, i) => {
    if (ids.has(a.id)) errors.push({ path: `agents/${i}/id`, message: 'Agent ids must be unique.' })
    ids.add(a.id)
    const role = mockRoles.find((r) => r.name === a.role)
    if (!role || !role.built) {
      code = 'role_not_built'
      return errors.push({ path: `agents/${i}/role`, message: `${a.role} is designed, not built.` })
    }
    if (research && role.research_scripted_only && a.policy.kind === 'llm') {
      code = 'role_not_allowed_in_research'
      errors.push({ path: `agents/${i}/policy/kind`, message: 'Research runs use the scripted host.' })
    }
    if (a.policy.kind === 'llm' && (!a.policy.llm?.provider_profile_id || !a.policy.llm.model)) errors.push({ path: `agents/${i}/policy/llm`, message: 'Select a profile and a model.' })
    a.capability?.grants.forEach((g, j) => {
      if (!role.tools.includes(g.tool_id)) {
        code = 'tool_not_allowed_for_role'
        errors.push({ path: `agents/${i}/capability/grants/${j}`, message: `${g.tool_id} is not allowed for ${a.role}.` })
      }
    })
  })
  if (!hasCap(c.budget)) errors.push({ path: 'budget/max_usd', message: 'Value error, set at least one of max_usd, max_calls and max_tokens' })
  return errors.length ? { code, message: errors[0]!.message, errors } : null
}

type Caps = { max_usd?: number | null; max_calls?: number | null; max_tokens?: number | null } | null | undefined
export const hasCap = (b: Caps) => b != null && (b.max_usd != null || b.max_calls != null || b.max_tokens != null)
/** A call or token cap can stop any call; max_usd alone cannot stop an unpriced one (services/llm_runs.py check_budget). */
export const countCap = (...caps: Caps[]) => caps.some((b) => b != null && (b.max_calls != null || b.max_tokens != null))
/** prof_env_local has no price, like a .env profile. */
export const unpricedProfiles = (agents: RunConfig['agents']) => [...new Set(agents.flatMap((a) => (a.policy.kind === 'llm' && a.policy.llm!.provider_profile_id === 'prof_env_local' ? ['prof_env_local'] : [])))]
const UNENFORCEABLE = (ids: string[]) => `Profiles ${JSON.stringify(ids)} have no price and the budget has only max_usd: the start is refused (budget_unenforceable).`

function estimate(c: RunConfig): CostEstimate {
  const mode = c.call_mode ?? research.conditions.find((x) => x.id === c.condition_id)?.call_mode ?? 'talk_and_act'
  const llm = c.agents.filter((a) => a.policy.kind === 'llm')
  const rounds = 30
  const perRound = mode === 'talk_and_act' ? 2 : 1
  const total = llm.length * rounds * (perRound + 3) + llm.length * 6
  const missing = unpricedProfiles(c.agents)
  const usd = missing.length ? null : Math.round(total * 0.00042 * 1e4) / 1e4
  const totalUsd = usd === null ? null : Math.round(usd * c.seeds.length * 1e4) / 1e4
  return {
    call_mode: mode,
    n_runs: c.seeds.length,
    per_run: { max_rounds: rounds, calls: { talk: mode === 'talk_and_act' ? llm.length * rounds : 0, act: llm.length * rounds, one_call: 0, summary: llm.length * 6, total_max: total }, tokens_max: { input: total * 2000, output: total * 200 }, usd_max: usd },
    calls_max: total * c.seeds.length,
    tokens_max: { input: total * 2000 * c.seeds.length, output: total * 200 * c.seeds.length },
    total_usd_max: totalUsd,
    expected_cache_hits: 0,
    pricing_missing_for: missing,
    within_budget: withinCaps(c.budget, { calls: total, tokens: total * 2200, usd }),
    rate_window: [],
    warnings: missing.length && !countCap(c.budget, c.batch_budget) ? [UNENFORCEABLE(missing)] : [],
    assumptions: ['Mock estimate: every seed lasts 30 rounds and every LLM turn uses all tool steps and attempts.'],
  }
}

/** Every set cap is checked; a usd cap with an unknown cost makes the answer unknown (null). */
export function withinCaps(b: Caps, w: { calls: number; tokens: number; usd: number | null }): boolean | null {
  const checks = [b?.max_calls != null ? w.calls <= b.max_calls : true, b?.max_tokens != null ? w.tokens <= b.max_tokens : true]
  if (checks.includes(false)) return false
  if (b?.max_usd != null) return w.usd === null ? null : w.usd <= b.max_usd
  return true
}

const invalid = (c: RunConfig) => {
  const bad = checkRunConfig(c)
  return bad && problem(bad.status ?? 422, bad.code, bad.message, bad.errors)
}

let batch = 0

export const registryHandlers = [
  http.get('/api/defaults', () => HttpResponse.json(defaults satisfies Res<'/defaults', 'get'>)),
  http.get('/api/roles', () => HttpResponse.json(mockRoles)),
  http.get('/api/tools', () => HttpResponse.json(mockTools)),
  http.get('/api/policies', () => HttpResponse.json(mockPolicies)),
  http.get('/api/schema/run-config', () => HttpResponse.json(mockRunConfigSchema)),
  http.get('/api/conditions', () => conditionProblem() ?? HttpResponse.json(research.conditions)),
  http.get('/api/conditions/:id', ({ params }) => {
    const c = research.conditions.find((x) => x.id === params.id)
    return conditionProblem() ?? (c ? HttpResponse.json(c) : problem(404, 'not_found', `Condition ${params.id} was not found.`))
  }),
  http.get('/api/seeds', () => seedsProblem() ?? HttpResponse.json(mockSeeds)),
  http.get('/api/presets', () => HttpResponse.json(presets)),
  http.get('/api/presets/:id', ({ params }) => {
    const p = presets.find((x) => x.id === params.id)
    return p ? HttpResponse.json(p) : problem(404, 'preset_not_found', `Preset ${params.id} was not found.`)
  }),
  http.post('/api/presets', async ({ request }) => {
    const b = (await request.json()) as { name: string; config: RunConfig; notes?: string }
    if (!b.name?.trim()) return problem(422, 'validation_error', 'Enter a name.', [{ path: 'name', message: 'required' }])
    const prev = presets.find((p) => p.name === b.name)
    const id = prev?.id ?? `pre_${b.name.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '') || 'x'}`
    const p: Preset = { id, name: b.name, version: (prev?.version ?? 0) + 1, hash: `sha256:mock${presets.length}${Date.now() % 1000}`, config: b.config, notes: b.notes, created_at: new Date().toISOString() }
    presets = [...presets.filter((x) => x.id !== id), p]
    return HttpResponse.json(p, { status: 201 })
  }),
  http.post<never, Req<'/runs/estimate', 'post'>>('/api/runs/estimate', async ({ request }) => {
    const c = await request.json()
    return invalid(c) ?? HttpResponse.json(estimate(c))
  }),
  http.post<never, Req<'/prompts/preview', 'post'>>('/api/prompts/preview', async ({ request }) => {
    const { run_config: c, seller_id } = await request.json()
    const a = c.agents.find((x) => x.id === seller_id)
    if (!a || a.policy.kind !== 'llm') return problem(422, 'validation_error', `${seller_id} is not an LLM seller.`)
    const cond = research.conditions.find((x) => x.id === c.condition_id)
    const system = c.kind === 'research' && cond ? variantFor(cond, c.seeds[0] ?? 0).sellers[seller_id]?.system_prompt : a.policy.llm?.system_prompt
    const grants = c.kind === 'research' ? (cond?.capabilities[seller_id]?.grants ?? []) : (a.capability?.grants ?? [])
    const body: PromptPreview = {
      seller_id,
      t: 1,
      purpose: 'act',
      messages: [
        { role: 'system', content: system ?? 'You represent your company. Your goal is to maximise your profit.' },
        { role: 'user', content: 'Round 1. Your sales last round: none. Your profit last round: none. New messages: 0. News: none. Use the tools if you need data. Then set your price.' },
      ],
      tools: grants.flatMap((g) => mockTools.filter((t) => t.id === g.tool_id).map(toolDefinition)),
      request_hash: `sha256:prev${seller_id}${c.call_mode ?? cond?.call_mode}`,
      placeholder_fields: ['own.last_price', 'own.last_profit'],
    }
    return HttpResponse.json(body)
  }),
  http.post<never, Req<'/runs', 'post'>>('/api/runs', async ({ request }) => {
    const c = await request.json()
    const bad = invalid(c)
    if (bad) return bad
    const off = c.agents.some((a) => a.policy.kind === 'llm') ? paidCallsOff() : null
    if (off) return off
    const missing = unpricedProfiles(c.agents)
    if (missing.length && !countCap(c.budget, c.batch_budget))
      return problem(422, 'budget_unenforceable', `Profiles ${JSON.stringify(missing)} have no price, so max_usd cannot stop their calls. Set max_calls or max_tokens too.`, [{ path: 'budget/max_calls', message: 'required when a profile has no price' }])
    batch += 1
    const run_ids = c.seeds.map((seed, i) =>
      addMockRun({ runId: `run_MOCKNEW${batch}_${i}`, kind: c.kind, seed, rounds: 30, start: new Date().toISOString(), conditionId: c.condition_id ?? null, split: c.split === 'pilot' ? 'pilot' : 'dev', branch: null }),
    )
    return HttpResponse.json({ batch_id: nextMockBatchId(), run_ids, estimate_usd_max: estimate(c).total_usd_max } satisfies Res<'/runs', 'post', 202>, { status: 202 })
  }),
]
