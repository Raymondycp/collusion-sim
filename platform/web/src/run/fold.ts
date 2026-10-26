import type { ChartMarkerKind, EpisodeHeader, EventOf, RunStatus, SimEvent } from '@/api/types'
import { usd } from '@/lib/format'

export type Point = { t: number; value: number }
/** Nash and joint optimum are null when the demand model has no closed forms (contract v0.5.0). */
export type ReferenceRow = { t: number; nash: number | null; joint_optimum: number | null }
export type Marker = {
  seq: number
  t: number
  kind: ChartMarkerKind
  agentId: string | null
  channelId: string | null
  text: string
}
export type Message = {
  seq: number
  t: number
  deliverT: number
  agentId: string
  channelId: string
  text: string
  /** posted.receivers (contract 0.21.0): the agents the post is delivered to. Null in older episodes. */
  receivers: string[] | null
}
export type NewsItem = { seq: number; t: number; text: string; visible: string[] }

export type RunState = {
  header: EpisodeHeader | null
  status: RunStatus | null
  /** Reason of the last `run_status_changed`, for example the error code of a run that waits as `recovering`. */
  statusReason: string | null
  endReason: EventOf<'run_ended'>['end_reason'] | null
  lastSeq: number
  /** First seq that the stream skipped. The socket reconnects from here. */
  gapFrom: number | null
  t: number | null
  /** Last round with a `settled` event. `t` can be one round ahead while a round is in progress (lead triage Q10). */
  settledT: number | null
  sellers: string[]
  prices: Record<string, Point[]>
  reference: ReferenceRow[]
  surplus: Point[]
  markers: Marker[]
  messages: Message[]
  news: NewsItem[]
  costUsd: number
  calls: number
  cacheHits: number
  /** Input + output tokens of the calls the budget counts: paid calls and charged replays, not plain cache hits (CR-24 m1).
   *  Cached input and reasoning tokens are inside them (contract 0.9.0). */
  tokens: number
  cachedInputTokens: number
  reasoningTokens: number
  /** Paid calls with cost_usd null (no price), so costUsd is a lower bound. */
  unpricedCalls: number
  fallbacks: number
  /** Turns that hit each limit (acted.limits_hit). */
  limitsHit: Record<string, number>
  events: SimEvent[]
}

export const emptyRunState: RunState = {
  header: null,
  status: null,
  statusReason: null,
  endReason: null,
  lastSeq: -1,
  gapFrom: null,
  t: null,
  settledT: null,
  sellers: [],
  prices: {},
  reference: [],
  surplus: [],
  markers: [],
  messages: [],
  news: [],
  costUsd: 0,
  calls: 0,
  cacheHits: 0,
  tokens: 0,
  cachedInputTokens: 0,
  reasoningTokens: 0,
  unpricedCalls: 0,
  fallbacks: 0,
  limitsHit: {},
  events: [],
}

function upsertByT<T extends { t: number }>(rows: T[], row: T) {
  const last = rows[rows.length - 1]
  if (last && last.t === row.t) rows[rows.length - 1] = row
  else rows.push(row)
}

function clone(s: RunState): RunState {
  const prices: Record<string, Point[]> = {}
  for (const [k, v] of Object.entries(s.prices)) prices[k] = v.slice()
  return {
    ...s,
    sellers: s.sellers.slice(),
    prices,
    limitsHit: { ...s.limitsHit },
    reference: s.reference.slice(),
    surplus: s.surplus.slice(),
    markers: s.markers.slice(),
    messages: s.messages.slice(),
    news: s.news.slice(),
    events: s.events.slice(),
  }
}

function interventionLabel(e: EventOf<'intervened'>): string {
  const i = e.intervention
  switch (i.type) {
    case 'probe':
      return `Probe: host sets ${i.target} to ${e.effective_price ?? i.price}`
    case 'placebo':
      return 'Placebo fork'
    case 'ablation':
      return i.ablated === 'none' ? 'Null ablation: nothing removed (noise floor)' : `Ablation: ${i.mode} ${i.channel_id}`
  }
}

const money = usd

/** Mutates `s`. Call it only on a fresh clone. */
function apply(s: RunState, e: SimEvent) {
  s.events.push(e)
  s.lastSeq = e.seq
  if (e.type !== 'run_started') s.t = e.t
  switch (e.type) {
    case 'run_started':
      s.header = e.header
      s.sellers = e.header.agents.filter((a) => a.role === 'seller').map((a) => a.id)
      for (const id of s.sellers) s.prices[id] ??= []
      break
    case 'round_started':
      upsertByT<ReferenceRow>(s.reference, { t: e.t, nash: e.reference.nash ?? null, joint_optimum: e.reference.joint_optimum ?? null })
      break
    case 'settled':
      s.settledT = e.t
      for (const [id, price] of Object.entries(e.prices)) {
        if (!s.prices[id]) {
          s.prices[id] = []
          s.sellers.push(id)
        }
        upsertByT(s.prices[id], { t: e.t, value: price })
      }
      break
    // Tier B, right after `settled` at the same t (contract v0.6.0).
    case 'reference':
      upsertByT<ReferenceRow>(s.reference, { t: e.t, nash: e.reference.nash ?? null, joint_optimum: e.reference.joint_optimum ?? null })
      upsertByT(s.surplus, { t: e.t, value: e.consumer_surplus })
      break
    case 'acted':
      for (const l of e.limits_hit ?? []) s.limitsHit[l] = (s.limitsHit[l] ?? 0) + 1
      if (e.fallback) {
        s.fallbacks += 1
        s.markers.push({ seq: e.seq, t: e.t, kind: 'fallback', agentId: e.agent_id, channelId: null, text: `${e.agent_id}: fallback, last price kept` })
      }
      break
    case 'posted':
      s.messages.push({ seq: e.seq, t: e.t, deliverT: e.deliver_t, agentId: e.agent_id, channelId: e.channel_id, text: e.text, receivers: e.receivers ?? null })
      s.markers.push({ seq: e.seq, t: e.t, kind: 'post', agentId: e.agent_id, channelId: e.channel_id, text: `${e.agent_id} posted in ${e.channel_id}` })
      break
    case 'news_published':
      s.news.push({ seq: e.seq, t: e.t, text: e.text, visible: e.visible })
      s.markers.push({ seq: e.seq, t: e.t, kind: 'news', agentId: null, channelId: null, text: e.text })
      break
    case 'intervened': {
      const i = e.intervention
      s.markers.push({
        seq: e.seq,
        t: e.t,
        kind: i.type,
        agentId: i.type === 'probe' ? i.target : null,
        channelId: i.type === 'ablation' ? (i.channel_id ?? null) : null,
        text: interventionLabel(e),
      })
      break
    }
    case 'llm_call':
      s.calls += 1
      if (e.cache_hit) s.cacheHits += 1
      s.costUsd += e.cost_usd ?? 0
      if (!e.cache_hit || e.charged) {
        s.tokens += (e.input_tokens ?? 0) + (e.output_tokens ?? 0)
        s.cachedInputTokens += e.cached_input_tokens ?? 0
        s.reasoningTokens += e.reasoning_tokens ?? 0
        if (e.cost_usd == null) s.unpricedCalls += 1
      }
      break
    case 'budget_warning':
      s.markers.push({ seq: e.seq, t: e.t, kind: 'budget', agentId: null, channelId: null, text: budgetText(e) })
      break
    case 'run_status_changed':
      s.status = e.status
      s.statusReason = e.reason ?? null
      break
    case 'run_ended':
      s.status = e.status
      s.endReason = e.end_reason
      if (e.cost_usd != null) s.costUsd = e.cost_usd
      break
    case 'observed':
    case 'tool_called':
    case 'planned':
    case 'outcome':
    case 'memory_summarised':
    case 'snapshot_saved':
    // The posted text comes in the `posted` event that follows; talk_text (tier B) is never shown as a message.
    case 'talk_replied':
    case 'talk_text':
      break
    default:
      e satisfies never
  }
}

/**
 * Folds a batch of events into a new state. Events must come in seq order.
 * A seq at or below `lastSeq` is a duplicate from a reconnect replay and is skipped.
 * A seq above `lastSeq + 1` is a gap: folding stops and `gapFrom` is set.
 */
/** The backend writes max_usd 0 when the budget has no max_usd; the event does not name the call or token cap that stopped the run (CR-24 m2). */
function budgetText(e: EventOf<'budget_warning'>): string {
  if (e.max_usd === 0) return e.level === 'p100' ? 'Budget reached (call or token cap)' : 'Budget at 80% (call or token cap)'
  return `Budget ${e.level === 'p100' ? 'reached' : 'at 80%'}: ${money(e.spent_usd)} of ${money(e.max_usd)}`
}

export function applyEvents(state: RunState, events: readonly SimEvent[]): RunState {
  if (events.length === 0 || state.gapFrom !== null) return state
  const next = clone(state)
  for (const e of events) {
    if (e.seq <= next.lastSeq) continue
    if (e.seq !== next.lastSeq + 1) {
      next.gapFrom = next.lastSeq + 1
      break
    }
    apply(next, e)
  }
  return next
}

export function clearGap(state: RunState): RunState {
  return state.gapFrom === null ? state : { ...state, gapFrom: null }
}

export const foldEvents = (events: readonly SimEvent[]) => applyEvents(emptyRunState, events)

const upTo = <T extends { t: number }>(rows: T[], t: number) => {
  let i = rows.length
  while (i > 0 && rows[i - 1]!.t > t) i--
  return i === rows.length ? rows : rows.slice(0, i)
}

/** The run as it was at the end of round `t`. Nothing after `t` leaks in. */
export function viewAt(state: RunState, t: number | null): RunState {
  if (t === null || state.t === null || t >= state.t) return state
  const prices: Record<string, Point[]> = {}
  for (const [k, v] of Object.entries(state.prices)) prices[k] = upTo(v, t)
  const events = state.events.filter((e) => e.t <= t)
  return {
    ...state,
    t,
    settledT: state.settledT === null ? null : Math.min(state.settledT, t),
    prices,
    reference: upTo(state.reference, t),
    surplus: upTo(state.surplus, t),
    markers: upTo(state.markers, t),
    messages: upTo(state.messages, t),
    news: upTo(state.news, t),
    events,
    lastSeq: events[events.length - 1]?.seq ?? -1,
  }
}

/** The last settled round: the timeline never goes to a round with no prices yet. */
export function lastT(state: RunState): number {
  return state.settledT ?? 0
}

export function firstT(state: RunState): number {
  return state.reference[0]?.t ?? 0
}
