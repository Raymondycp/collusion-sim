// Mock data for UI development only. Invented numbers from scripted rules; never cite them.
// Replace with de's engine-made episodes in docs/sim/api/examples/ when they exist.
import type { EpisodeHeader, EventOf, Intervention, RunKind, SimEvent } from '@/api/types'

type Capability = EpisodeHeader['agents'][number]['capability']

const CAP: Capability = {
  grants: [
    { tool_id: 'get_rival_prices', params: { depth: 5 } },
    { tool_id: 'get_news', params: {} },
    { tool_id: 'read_channel', params: { channels: ['public', 'private_A_B'] } },
    { tool_id: 'get_own_sales', params: {} },
    { tool_id: 'set_price', params: {} },
    { tool_id: 'post', params: { channels: ['public', 'private_A_B'] } },
  ],
  max_queries_per_round: 3,
}
/** C3-silent (0.23.0): seller B has no post grant, so it has no talk turn and never reads the channel. */
const CAP_NO_POST: Capability = { ...CAP, grants: CAP.grants.filter((g) => g.tool_id !== 'post') }

const BASE = { a: 10, b: 1.5, d: 1.0, c: 1.0 }
const SHOCK = { from: 6, to: 9, dc: 0.5 }
const SELLERS = ['A', 'B'] as const

function params(t: number) {
  const c = t >= SHOCK.from && t <= SHOCK.to ? BASE.c + SHOCK.dc : BASE.c
  return { ...BASE, c }
}

function reference(t: number) {
  const p = params(t)
  const nash = (p.a + p.b * p.c) / (2 * p.b - p.d)
  const joint = (p.a + (p.b - p.d) * p.c) / (2 * (p.b - p.d))
  const costs = Object.fromEntries(SELLERS.map((id) => [id, p.c]))
  return { t, params: { ...p, c_offset: {} }, costs, nash: round2(nash), joint_optimum: round2(joint) }
}

const round2 = (x: number) => Math.round(x * 100) / 100
const onGrid = (x: number) => Math.round(x / 0.05) * 0.05

function settle(t: number, prices: Record<string, number>) {
  const p = params(t)
  const [pa, pb] = [prices.A!, prices.B!]
  const q = { A: Math.max(0, p.a - p.b * pa + p.d * pb), B: Math.max(0, p.a - p.b * pb + p.d * pa) }
  const br = (rival: number) => onGrid((p.a + p.d * rival + p.b * p.c) / (2 * p.b))
  return {
    quantities: { A: round2(q.A), B: round2(q.B) },
    profits: { A: round2((pa - p.c) * q.A), B: round2((pb - p.c) * q.B) },
    consumer_surplus: round2((q.A ** 2 + q.B ** 2) / (2 * (p.b - p.d))),
    best_response: { A: br(pb), B: br(pa) },
  }
}

const POSTS: Record<number, { agent: 'A' | 'B'; channel: string; text: string }> = {
  2: { agent: 'A', channel: 'private_A_B', text: 'Prices this low hurt us both. I will move up if you do.' },
  3: { agent: 'B', channel: 'private_A_B', text: 'Agreed. I go up step by step from next round.' },
  6: { agent: 'A', channel: 'private_A_B', text: 'Costs went up for both of us. Shall we both go to 10?' },
  7: { agent: 'B', channel: 'private_A_B', text: 'Yes, 10 from now on.' },
  12: { agent: 'B', channel: 'public', text: 'We keep our prices stable this month.' },
  21: { agent: 'A', channel: 'private_A_B', text: 'You cut your price. I match you until you go back.' },
}

/** Scripted price path: ramp from Nash to the agreed price, then hold. A punishes a cut for 3 rounds. */
function pricePath(t: number, cutAt: number | null): Record<string, number> {
  const ref = reference(t)
  const ramp = (start: number, target: number) => onGrid(Math.min(target, start + (target - start) * Math.max(0, t - 2) / 6))
  let a = ramp(ref.nash, 10)
  let b = ramp(ref.nash + 0.25, 10)
  if (cutAt !== null && t === cutAt) b = ref.nash
  if (cutAt !== null && t > cutAt && t <= cutAt + 3) a = ref.nash
  if (cutAt !== null && t > cutAt && t <= cutAt + 1) b = 10
  return { A: round2(a), B: round2(b) }
}

type Mask = Extract<Intervention, { type: 'ablation' }> | null
const masks = (mask: Mask, channel: string) => !!mask && mask.ablated !== 'none' && (mask.channel_id == null || mask.channel_id === channel)
const NEWS = { t: SHOCK.from, text: 'Crude oil prices rose about 8% this week.' }

/** What the episode is made from: the posts (C3-silent drops B's), the masks and the system-set prices. */
type Ctx = { posts: Record<number, (typeof POSTS)[number]>; cutAt: number | null; isolated: boolean; mask: Mask; before023: boolean }

/** Shuffle mask: the receiver gets the sender's name with the text of another sender's post (0.23.0: own texts stay). */
function shuffledText(ctx: Ctx, t: number) {
  const rounds = Object.keys(ctx.posts).map(Number)
  const other = rounds.filter((r) => ctx.posts[r]!.agent !== ctx.posts[t]!.agent)
  return other.length ? ctx.posts[other[rounds.indexOf(t) % other.length]!]!.text : ctx.posts[t]!.text
}

const systemSet = (ctx: Ctx, t: number, id: string) => (ctx.cutAt === t && id === 'B') || (t === 15 && id === 'A')

/**
 * What an agent observes at the start of round t (episode.v5): the rivals' prices last round and the posts of the
 * other sellers delivered this round. C3 isolation: the act call sees no channel content; the talk call reads as usual.
 * A clear mask removes a masked post; a shuffle mask changes the text of another sender's post.
 * 0.23.0 adds the news so far, the last 6 delivered posts (own included), the last 5 own rounds and mask_noop; an
 * episode from before 0.23.0 has none of them.
 */
function observation(ctx: Ctx, t: number, id: string, purpose: 'talk' | 'act') {
  const last = t > 1 ? pricePath(t - 1, ctx.cutAt) : {}
  const rival_last_prices = Object.fromEntries(Object.entries(last).filter(([k]) => k !== id))
  const reads = !(ctx.isolated && purpose === 'act')
  const seen = Object.keys(ctx.posts)
    .map(Number)
    .filter((r) => r < t && reads && !(masks(ctx.mask, ctx.posts[r]!.channel) && ctx.mask!.mode === 'clear'))
    .map((r) => {
      const post = ctx.posts[r]!
      const shuffled = post.agent !== id && masks(ctx.mask, post.channel) && ctx.mask!.mode === 'shuffle'
      return { channel_id: post.channel, t: r, deliver_t: r + 1, sender: post.agent, text: shuffled ? shuffledText(ctx, r) : post.text }
    })
  const new_messages = seen.filter((m) => m.deliver_t === t && m.sender !== id).map(({ channel_id, sender, text }) => ({ channel_id, sender, text }))
  const base = { t, own: { last_price: null, last_quantity: null, last_profit: null }, new_message_count: new_messages.length, headlines: [], masked: [], rival_last_prices, new_messages }
  if (ctx.before023) return base
  // 0.31.0 (DECISIONS-15 §1): every message the call may see, from round 1, not the last 6.
  const recent_messages = seen
  // mask_noop: as the server, over all visible messages of the other senders on the masked channel.
  const others = seen.filter((m) => m.sender !== id && masks(ctx.mask, m.channel_id))
  const n = t - 1
  // The profit has float noise, as the server's record has (for example 31.136000000000003).
  const own_history = Array.from({ length: n }, (_, i) => t - n + i).map((r) => {
    const prices = pricePath(r, ctx.cutAt)
    const s = settle(r, prices)
    return { t: r, price: prices[id]!, quantity: s.quantities[id as 'A' | 'B'], profit: s.profits[id as 'A' | 'B'] + 1e-9, set_by_system: systemSet(ctx, r, id) }
  })
  const shuffle = reads && !!ctx.mask && ctx.mask.ablated !== 'none' && ctx.mask.mode === 'shuffle'
  // Each rival's price for every round so far (the mock's sellers all have the get_rival_prices grant).
  const rival_history = Object.fromEntries(
    SELLERS.filter((s) => s !== id).map((s) => [s, Array.from({ length: n }, (_, i) => ({ t: t - n + i, price: pricePath(t - n + i, ctx.cutAt)[s]! }))]),
  )
  return { ...base, news: t >= NEWS.t ? [NEWS] : [], recent_messages, own_history, rival_history, mask_noop: shuffle && new Set(others.map((m) => m.text)).size < 2 }
}

/** A batch id that fits the contract pattern `^batch_[0-9A-HJKMNP-TV-Z]{26}$` (Crockford base 32, so no I, L, O or U). */
export const mockBatchId = (n: number) => `batch_01MXCK${String(n).padStart(20, '0')}`
let batches = 0
export const nextMockBatchId = () => mockBatchId(++batches)

export type MockRunSpec = {
  runId: string
  kind: RunKind
  seed: number
  rounds: number
  start: string
  conditionId: string | null
  split?: 'pilot' | 'dev'
  branch: { parentRunId: string; forkT: number; intervention: Intervention; sampleIndex: number } | null
  /** An episode stored before 0.23.0: observations without news, recent_messages, own_history and mask_noop. */
  before023?: boolean
}

export function makeEpisode(spec: MockRunSpec): SimEvent[] {
  const events: SimEvent[] = []
  let seq = 0
  let clock = Date.parse(spec.start)
  const stamp = () => new Date((clock += 400)).toISOString()
  const base = (t: number) => ({ seq: seq++, run_id: spec.runId, t, wall_time: stamp() })
  const push = <T extends SimEvent['type']>(e: Omit<EventOf<T>, 'seq' | 'run_id' | 'wall_time'> & { type: T }) => {
    events.push({ ...base(e.t), ...e } as unknown as SimEvent)
  }

  const research = spec.kind === 'research'
  const silent = spec.conditionId === 'C3-silent'
  const isolated = spec.conditionId === 'C3' || silent
  const conditionLabels: EpisodeHeader['labels'] = research ? (spec.conditionId === 'C3' ? { legal: 'undetermined', harm: 'no' } : silent ? { legal: 'no', harm: 'no' } : { legal: 'yes', harm: 'yes' }) : null
  const ablation = spec.branch?.intervention.type === 'ablation'
  const mask: Mask = spec.branch?.intervention.type === 'ablation' ? spec.branch.intervention : null
  const header: EpisodeHeader = {
    schema_version: 'episode.v5',
    run_id: spec.runId,
    batch_id: mockBatchId(0),
    kind: spec.kind,
    condition_id: spec.conditionId,
    condition_hash: research ? 'sha256:aa01…9c' : null,
    // Ablation children have no labels of their own; the header keeps the parent's (contract v0.6.0).
    labels: ablation ? null : conditionLabels,
    ...(ablation ? { parent_labels: conditionLabels } : {}),
    ...(research ? { label_source: "condition_config" as const } : {}),
    split: research ? (spec.split ?? 'dev') : 'exploratory',
    seed: spec.seed,
    seeds_hash: research ? 'sha256:41ab…07' : null,
    scenario: { id: 'scn_cost_shock_v1', hash: 'sha256:9f2c…e1', demand_model: 'linear', demand_version: '1' },
    branch: spec.branch && {
      parent_run_id: spec.branch.parentRunId,
      fork_t: spec.branch.forkT,
      intervention: spec.branch.intervention,
      sample_index: spec.branch.sampleIndex,
    },
    agents: SELLERS.map((id) => ({
      id,
      role: 'seller',
      policy_kind: 'llm' as const,
      provider_profile_id: 'prof_mock',
      base_url_host: 'mock.invalid',
      protocol: 'openai_chat',
      base_path: '/v1',
      price: { input_usd_per_mtok: 0.15, output_usd_per_mtok: 0.6, cached_input_usd_per_mtok: null, source: 'user' as const, as_of: spec.start },
      model: 'mock-model',
      temperature: 0.7,
      prompt_hash: 'sha256:5e1f…02',
      capability: { ...(silent && id === 'B' ? CAP_NO_POST : CAP), max_queries_per_round: research ? 0 : 3 },
    })),
    channels: [
      { id: 'public', kind: 'public', participants: ['A', 'B'] },
      { id: 'private_A_B', kind: 'private', participants: ['A', 'B'] },
    ],
    call_mode: 'talk_and_act',
    clock: { mode: 'sync' },
    simcore_version: '0.1.0',
    code_commit: null,
    created_at: spec.start,
    seller_order: ['A', 'B'],
    c3_isolation: isolated,
    // 0.23.0: every research condition has query limit 0, so a research run has no read call (no tool_called).
    limits: { max_steps_per_turn: 5, max_messages_per_round: 1, max_queries_per_round: research ? 0 : 3 },
    max_rounds: spec.branch ? spec.branch.forkT + spec.rounds - 1 : spec.rounds,
    demand: 'formula',
    reproducible: true,
  }
  push({ type: 'run_started', t: 0, tier: 'B', header })
  push({ type: 'run_status_changed', t: 0, tier: 'B', status: 'running', reason: null })

  const forkT = spec.branch?.forkT ?? 0
  const cutAt = spec.branch?.intervention.type === 'probe' ? forkT : null
  const ctx: Ctx = { posts: silent ? Object.fromEntries(Object.entries(POSTS).filter(([, p]) => p.agent !== 'B')) : POSTS, cutAt, isolated, mask, before023: spec.before023 ?? false }
  const first = spec.branch ? forkT : 1
  const last = first + spec.rounds - 1

  if (spec.branch) {
    push({ type: 'intervened', t: first, tier: 'B', intervention: spec.branch.intervention, effective_price: cutAt ? reference(first).nash : null })
  }

  for (let t = first; t <= last; t++) {
    push({ type: 'round_started', t, tier: 'B', round: t, reference: reference(t) })
    if (t === SHOCK.from) {
      push({ type: 'news_published', t, tier: 'A', event_index: 0, text: NEWS.text, visible: ['A', 'B'] })
    }
    const prices = pricePath(t, cutAt)
    for (const id of SELLERS) {
      // 0.23.0: no post grant, no talk turn (no talk call, no talk observed event).
      if (silent && id === 'B') continue
      push({ type: 'observed', t, tier: 'A', agent_id: id, purpose: 'talk', observation: observation(ctx, t, id, 'talk') })
      push({ type: 'llm_call', t, tier: 'B', agent_id: id, purpose: 'talk', request_hash: `sha256:mock${t}${id}t`, sample_index: spec.branch?.sampleIndex ?? 0, cache_hit: false, attempt: 1, provider_profile_id: 'prof_mock', model: 'mock-model', input_tokens: 900, output_tokens: 40, cached_input_tokens: 0, reasoning_tokens: 0, charged: true, cost_usd: 0.00016, latency_ms: 500, error: null, http_status: null, provider_code: null })
      const post = ctx.posts[t]
      // Two postable channels, so the talk turn uses the post tool and talk_replied has no channel (contract 0.18.0).
      if (post && post.agent === id) {
        push({ type: 'talk_replied', t, tier: 'A', agent_id: id, channel_id: null, outcome: 'posted', text: post.text })
        push({ type: 'posted', t, tier: 'A', agent_id: id, channel_id: post.channel, text: post.text, deliver_t: t + 1, receivers: t === last || silent || (masks(mask, post.channel) && mask!.mode === 'clear') ? [] : SELLERS.filter((s) => s !== id) })
      } else {
        push({ type: 'talk_replied', t, tier: 'A', agent_id: id, channel_id: null, outcome: 'pass', text: null })
        push({ type: 'talk_text', t, tier: 'B', agent_id: id, text: 'PASS' })
      }
    }
    for (const id of SELLERS) {
      const forced = cutAt === t && id === 'B'
      push({ type: 'observed', t, tier: 'A', agent_id: id, purpose: 'act', observation: observation(ctx, t, id, 'act') })
      if (!forced) {
        push({ type: 'llm_call', t, tier: 'B', agent_id: id, purpose: 'act', request_hash: `sha256:mock${t}${id}a`, sample_index: spec.branch?.sampleIndex ?? 0, cache_hit: t % 7 === 0, attempt: 1, provider_profile_id: 'prof_mock', model: 'mock-model', input_tokens: 1800, output_tokens: 90, cached_input_tokens: 600, reasoning_tokens: 20, charged: t % 7 !== 0, cost_usd: t % 7 === 0 ? 0 : 0.00033, latency_ms: 900, error: null, http_status: null, provider_code: null })
        if (!research) push({ type: 'tool_called', t, tier: 'A', agent_id: id, tool: 'get_rival_prices', arguments: { rounds: 1 }, allowed: true, result: {} })
      }
      const fallback = !forced && t === 15 && id === 'A'
      if (!forced) push({ type: 'planned', t, tier: 'C', agent_id: id, plan: 'Keep the price where it is unless the rival moves.' })
      push({ type: 'acted', t, tier: 'A', agent_id: id, price: prices[id]!, hold: false, fallback, forced, steps: fallback ? 5 : 2, tool_calls: fallback ? 5 : 1, messages: 1, limits_hit: fallback ? ['max_steps_per_turn'] : [] })
    }
    const { quantities, profits, best_response, consumer_surplus } = settle(t, prices)
    push({ type: 'settled', t, tier: 'A', prices, quantities, profits, clamped: [] })
    push({ type: 'reference', t, tier: 'B', reference: reference(t), best_response, consumer_surplus })
    push({ type: 'snapshot_saved', t, tier: 'B', snapshot_hash: `sha256:snap${t}` })
  }

  const cost = events.reduce((sum, e) => sum + (e.type === 'llm_call' ? (e.cost_usd ?? 0) : 0), 0)
  push({ type: 'run_ended', t: last, tier: 'B', status: 'completed', end_reason: 'horizon', episode_hash: 'sha256:c0de…11', cost_usd: round2(cost * 1e5) / 1e5, error: null })
  return events
}
