import type { EventOf, RunStatus, SimEvent } from '@/api/types'
import { lastT, type Marker, type Message, type RunState } from '@/run/fold'

/** The last settled round of a run that has ended, with how it ended. A post of that round has no later round to deliver it. */
export type Final = { t: number; status: string }

/** Null while the run goes on (a live run, a paused one, one that waits to recover), and for a run with no status yet. */
export function finalRound(full: RunState, status: RunStatus | null | undefined): Final | null {
  return status && ENDED.has(status) ? { t: lastT(full), status } : null
}

export type Channel = { id: string; kind: 'public' | 'private'; participants: string[] }

/** Header channels, plus a channel seen only in posts (its participants are then unknown). */
export function channelsOf(state: RunState): Channel[] {
  const out: Channel[] = (state.header?.channels ?? []).flatMap((c) => (c.id && c.kind ? [{ id: c.id, kind: c.kind, participants: c.participants ?? [] }] : []))
  for (const id of new Set(state.messages.map((m) => m.channelId))) {
    if (!out.some((c) => c.id === id)) out.push({ id, kind: id === 'public' ? 'public' : 'private', participants: [] })
  }
  return out
}

export type InboxItem = Omit<Message, 'text'> & {
  direction: 'in' | 'out'
  /** The text this agent sent or saw. Null in a shuffle run while the received text is not known (see `pending`). */
  text: string | null
  /** Null text because the receiver has not observed the delivery round yet (a live run); else it is not recorded. */
  pending: boolean
  /** Channel members other than the sender, for a post with no recorded receivers. */
  members: string[]
  /** The incoming text comes from the receiver's observed event, not from the post (shuffle mask). */
  asReceived: boolean
  /** An outgoing post that reached nobody: the channel members that could not read it, and why. */
  unseenBy: Unseen[]
  /** An outgoing post on the channel that the run's clear mask removes. */
  cleared: boolean
}

/** no_talk_turn: c3_isolation and no talk call (C3-silent's B). no_read_grant: no read_channel grant that names the channel. */
export type Unseen = { agentId: string; cause: 'no_talk_turn' | 'no_read_grant' }

type ObservedIndex = {
  /** The observed events by `agent|round`, in call order. */
  byRound: Map<string, EventOf<'observed'>[]>
  /** Agents with at least one observed event, and those with a talk or single call. */
  observers: Set<string>
  talkers: Set<string>
  /** `agent|channel` for each channel of which an observed event of the agent holds a message. */
  seen: Set<string>
}

const observedCache = new WeakMap<SimEvent[], ObservedIndex>()

/** One pass over the observed events, once per event list. A folded state never changes its event list. */
function observedIndex(state: RunState): ObservedIndex {
  let index = observedCache.get(state.events)
  if (!index) {
    index = { byRound: new Map(), observers: new Set(), talkers: new Set(), seen: new Set() }
    for (const e of state.events) {
      if (e.type !== 'observed') continue
      const key = `${e.agent_id}|${e.t}`
      index.byRound.set(key, [...(index.byRound.get(key) ?? []), e])
      index.observers.add(e.agent_id)
      if (e.purpose === 'talk' || e.purpose === 'one_call') index.talkers.add(e.agent_id)
      for (const m of [...(e.observation.recent_messages ?? []), ...(e.observation.new_messages ?? [])]) index.seen.add(`${e.agent_id}|${m.channel_id}`)
    }
    observedCache.set(state.events, index)
  }
  return index
}

/** What the agent saw in round t: its observed events, in call order (talk, then act). */
export const observationsAt = (state: RunState, agentId: string, t: number): EventOf<'observed'>[] => observedIndex(state).byRound.get(`${agentId}|${t}`) ?? []

const grantsOf = (state: RunState, agentId: string) => state.header?.agents.find((a) => a.id === agentId)?.capability.grants

/**
 * Under c3_isolation only the talk call reads a channel, so a seller with no talk turn reads none (C3-silent's B).
 * The observed events decide: no talk call in round t, or in any round when t is not given. Without observed events
 * (a live run, a post in the last round), the grants decide: no `post` grant means no talk turn.
 */
export function noTalkTurn(state: RunState, agentId: string, t?: number): boolean {
  if (!state.header?.c3_isolation) return false
  if (t === undefined) {
    const index = observedIndex(state)
    if (index.observers.has(agentId)) return !index.talkers.has(agentId)
  } else {
    const calls = observationsAt(state, agentId, t)
    if (calls.length) return !calls.some((e) => e.purpose === 'talk' || e.purpose === 'one_call')
  }
  const grants = grantsOf(state, agentId)
  return !!grants && !grants.some((g) => g.tool_id === 'post')
}

/**
 * Why a member did not get a post with `receivers: []`. SPEC: the receivers are the members with a read_channel grant
 * that names the channel, and under c3_isolation only the talk call reads. Null: the member reads the channel, so
 * another cause (a mask, the last round) applies.
 */
function unseenCause(state: RunState, member: string, channelId: string, deliverT: number): Unseen['cause'] | null {
  if (noTalkTurn(state, member, deliverT)) return 'no_talk_turn'
  if (observedIndex(state).seen.has(`${member}|${channelId}`)) return null
  const grants = grantsOf(state, member)
  const reads = grants?.some((g) => g.tool_id === 'read_channel' && ((g.params as { channels?: string[] }).channels ?? []).includes(channelId))
  return grants && !reads ? 'no_read_grant' : null
}

/** The run's clear mask removes the posts on this channel that are delivered from the fork on (as View.receivers). */
function clearedBy(state: RunState, m: Message): boolean {
  const b = state.header?.branch
  const i = b?.intervention
  return !!b && i?.type === 'ablation' && i.ablated !== 'none' && i.mode === 'clear' && i.channel_id === m.channelId && m.deliverT >= b.fork_t
}

/** A shuffle mask gives the receiver the sender's name with another message's text (UI-WORKBENCH.md §2, CR-50). */
export function isShuffleRun(state: RunState): boolean {
  const i = state.header?.branch?.intervention
  return i?.type === 'ablation' && i.ablated !== 'none' && i.mode === 'shuffle'
}

const ENDED = new Set(['completed', 'stopped', 'failed'])

/**
 * The secret-channel messages one agent sends and gets, in delivery order, as of round `cursorT`.
 * An incoming message shows from its delivery round. The agent's first observed event of that round decides the
 * delivery when it lists new_messages (0.18.0): the entry of the same channel and sender, matched in post order.
 * Without it, posted.receivers decides; an older post without them falls back to the channel members, which
 * C3 isolation can make wider than the real readers.
 */
export function inbox(state: RunState, agentId: string, cursorT: number): InboxItem[] {
  const channels = new Map(channelsOf(state).map((c) => [c.id, c]))
  const shuffle = isShuffleRun(state)
  const order = new Map<string, number>()
  return state.messages
    .flatMap((m): InboxItem[] => {
      const c = channels.get(m.channelId)
      if (!c || c.kind !== 'private') return []
      const members = c.participants.filter((p) => p !== m.agentId)
      if (m.agentId === agentId) {
        if (m.t > cursorT) return []
        const unseenBy = m.receivers?.length === 0 ? members.flatMap((p): Unseen[] => { const cause = unseenCause(state, p, m.channelId, m.deliverT); return cause ? [{ agentId: p, cause }] : [] }) : []
        return [{ ...m, direction: 'out', members, asReceived: false, pending: false, unseenBy, cleared: clearedBy(state, m) }]
      }
      if (m.deliverT > cursorT) return []
      const key = `${m.agentId}|${m.channelId}|${m.deliverT}`
      const k = order.get(key) ?? 0
      order.set(key, k + 1)
      const obs = observationsAt(state, agentId, m.deliverT)[0]
      const seen = obs?.observation.new_messages
      const entry = seen?.filter((n) => n.channel_id === m.channelId && n.sender === m.agentId)[k]
      if (seen ? !entry : !(m.receivers ?? members).includes(agentId)) return []
      const pending = shuffle && !obs && !ENDED.has(state.status ?? '')
      return [{ ...m, direction: 'in', members, asReceived: shuffle, pending, text: shuffle ? (entry?.text ?? null) : m.text, unseenBy: [], cleared: false }]
    })
    .sort((a, b) => (a.direction === 'in' ? a.deliverT : a.t) - (b.direction === 'in' ? b.deliverT : b.t) || a.seq - b.seq)
}

export function publicMessages(state: RunState, cursorT: number): Message[] {
  const pub = new Set(channelsOf(state).filter((c) => c.kind === 'public').map((c) => c.id))
  return state.messages.filter((m) => pub.has(m.channelId) && m.t <= cursorT)
}

/** News that not every seller sees. Public view hides it (UI-WORKBENCH.md §2, public view rules). */
export function isRestricted(state: RunState, visible: string[]): boolean {
  return state.sellers.some((s) => !visible.includes(s))
}

/**
 * Public view (UI-WORKBENCH.md §2): what a stakeholder sees. A post on a non-public channel and restricted news are left out,
 * also as a chart marker or a log line, because a marker or a line shows that the secret message or the news exists.
 * A price action shows only after its round settles (CR-49).
 */
function publicFilter(state: RunState) {
  const pub = new Set(channelsOf(state).filter((c) => c.kind === 'public').map((c) => c.id))
  const restricted = new Set(state.news.filter((n) => isRestricted(state, n.visible)).map((n) => n.seq))
  return {
    channel: (channelId: string | null) => channelId !== null && pub.has(channelId),
    marker: (m: Marker) => (m.kind === 'post' ? m.channelId !== null && pub.has(m.channelId) : !(m.kind === 'news' && restricted.has(m.seq))),
    event: (e: SimEvent) =>
      e.type === 'posted'
        ? pub.has(e.channel_id)
        : e.type === 'acted'
          ? e.t <= (state.settledT ?? -1)
          : !(e.type === 'news_published' && restricted.has(e.seq)),
  }
}

export const publicMarkers = (state: RunState, markers: Marker[]): Marker[] => markers.filter(publicFilter(state).marker)
export const publicEvents = (state: RunState, events: SimEvent[]): SimEvent[] => events.filter(publicFilter(state).event)
export const onPublicChannel = (state: RunState, channelId: string) => publicFilter(state).channel(channelId)

/**
 * The round the cards, channels and log show. Following a live run, this is the round in progress (one after the last
 * settled round while the agents act), so a card can say "thinking". The charts and metrics stay at the settled round.
 */
export function panelRound(state: RunState, t: number | null): number {
  const settled = state.settledT ?? 0
  return t === null ? Math.max(settled, state.t ?? 0) : Math.min(t, settled)
}

export type AgentRound = {
  price: number | null
  action: 'set' | 'hold' | 'fallback' | null
  /** No `acted` event yet in this round of a live run. */
  thinking: boolean
  calls: number
}

/** The footer of an agent card: its price and action in round `t`, and its model calls up to `t`. */
export function agentRound(state: RunState, agentId: string, t: number, publicView = false): AgentRound {
  let acted: EventOf<'acted'> | undefined
  let calls = 0
  let started = false
  for (const e of state.events) {
    if (e.t > t) break
    if (e.type === 'llm_call' && e.agent_id === agentId) calls++
    if (e.t !== t) continue
    if (e.type === 'round_started') started = true
    if (e.type === 'acted' && e.agent_id === agentId) acted = e
  }
  const settled = state.prices[agentId]?.find((p) => p.t === t)?.value ?? null
  return {
    // Public view: no pending price before the round settles (CR-49).
    price: settled ?? (publicView ? null : (acted?.price ?? null)),
    action: acted ? (acted.fallback ? 'fallback' : acted.hold ? 'hold' : 'set') : null,
    thinking: started && !acted && state.status === 'running',
    calls,
  }
}

export type PricePair = { t: number; own: number; rival: number }

/** Market chart 2: one point per settled round, this seller's price against the mean of the other sellers' prices. */
export function pricePairs(state: RunState, seller: string): PricePair[] {
  const rivals = state.sellers.filter((s) => s !== seller)
  if (!rivals.length) return []
  const at = (id: string) => new Map((state.prices[id] ?? []).map((p) => [p.t, p.value]))
  const rivalAt = rivals.map(at)
  return (state.prices[seller] ?? []).flatMap((p) => {
    const r = rivalAt.map((m) => m.get(p.t))
    return r.every((v) => v !== undefined) ? [{ t: p.t, own: p.value, rival: (r as number[]).reduce((a, b) => a + b, 0) / r.length }] : []
  })
}

/** One line per event for the log. Returns null for events the log leaves out (they have no round meaning). */
export function logLine(e: SimEvent): string | null {
  switch (e.type) {
    case 'run_started':
      return 'Run started'
    case 'round_started':
      return 'Round started'
    case 'news_published':
      return `News: ${e.text}`
    case 'posted':
      return `${e.agent_id} posted in ${e.channel_id}`
    case 'acted':
      return `${e.agent_id} ${e.fallback ? 'fell back (last price kept)' : e.hold ? 'held' : `set ${e.price}`}`
    case 'settled':
      return `Settled: ${Object.entries(e.prices)
        .map(([k, v]) => `${k} ${v}`)
        .join(', ')}`
    case 'llm_call':
      return `${e.agent_id} model call (${e.purpose})${e.cache_hit ? ', cached' : ''}${e.error ? `, ${e.error}` : ''}`
    case 'intervened':
      return `Intervention: ${e.intervention.type}`
    case 'budget_warning':
      return `Budget ${e.level === 'p100' ? 'reached' : 'at 80%'}`
    case 'run_status_changed':
      return `Status: ${e.status}`
    case 'run_ended':
      return `Run ended: ${e.status}`
    default:
      return null
  }
}
