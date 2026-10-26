import { useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { sellerColor } from '@/components/chart/PriceChart'
import type { EventOf, RunDetail } from '@/api/types'
import type { RunState } from '@/run/fold'
import { cn } from '@/lib/utils'
import { PURPOSE_TEXT } from './labels'
import { agentRound, inbox, isRestricted, logLine, noTalkTurn, observationsAt, onPublicChannel, publicEvents, publicMessages, type Final, type InboxItem } from './derive'

/** A scroll box that follows new items while the reader is at the bottom, and stays put when they scrolled up. */
function FollowList({ count, children, label }: { count: number; children: ReactNode; label: string }) {
  const list = useRef<HTMLOListElement>(null)
  const atBottom = useRef(true)
  useLayoutEffect(() => {
    if (list.current && atBottom.current) list.current.scrollTop = list.current.scrollHeight
  }, [count])
  return (
    <ol
      ref={list}
      aria-label={label}
      onScroll={(e) => {
        const el = e.currentTarget
        atBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 8
      }}
      className="max-h-56 min-h-16 space-y-1.5 overflow-y-auto p-2">
      {children}
    </ol>
  )
}

function Card({ title, children, footer, label }: { title: ReactNode; children: ReactNode; footer?: ReactNode; label: string }) {
  return (
    <section aria-label={label} className="flex min-w-0 flex-col rounded-lg border">
      <h3 className="flex items-center gap-2 border-b px-3 py-2 text-sm font-medium">{title}</h3>
      <div className="min-h-0 flex-1">{children}</div>
      {footer && <div className="border-t px-3 py-1.5 text-xs text-muted-foreground">{footer}</div>}
    </section>
  )
}

const ACTION_TEXT = { set: 'acted', hold: 'held', fallback: 'fallback hold' } as const

/** Prices, units and profits to 2 decimals: the record keeps float noise such as 31.136000000000003. */
const n2 = (x: number) => Number(x.toFixed(2))

/**
 * The head of an outgoing post with `receivers: []`: each cause that the record shows. A post of the last settled round of a run
 * that ended has no later round to deliver it, and says so. A run that completed has a last round. A run that stopped or failed
 * may not have finished the round after it, so the text names how it ended (CR-87 minor 5). Else "delivered to nobody".
 */
function undelivered(m: InboxItem, final: Final | null): string {
  const who = (cause: string) => m.unseenBy.filter((u) => u.cause === cause).map((u) => u.agentId)
  const has = (ids: string[]) => `${ids.join(', ')} ${ids.length > 1 ? 'have' : 'has'}`
  const talk = who('no_talk_turn')
  const read = who('no_read_grant')
  const why = [
    m.cleared ? 'the clear mask removes this channel' : null,
    talk.length ? `${has(talk)} no talk turn (offer unseen)` : null,
    read.length ? `${has(read)} no read grant for this channel` : null,
  ].filter((x) => x !== null)
  if (why.length) return `R${m.t}, not delivered: ${why.join(', and ')}`
  if (m.t !== final?.t) return `R${m.t}, delivered to nobody`
  return final.status === 'completed'
    ? `R${m.t}, posted in the last round: the run ended before any round could deliver it`
    : `R${m.t}, posted in the last settled round: the run ${final.status} before any round could deliver it`
}

type OnTrail = ((seq: number) => void) | undefined

/** Opens the trail of one llm_call. The same label in the Log and in the fold, so both name the same call. */
function TrailButton({ call, onTrail }: { call: EventOf<'llm_call'>; onTrail: (seq: number) => void }) {
  return (
    <button type="button" aria-label={`Trail of call ${call.seq}`} onClick={() => onTrail(call.seq)} className="shrink-0 rounded border px-1.5 text-[11px] text-muted-foreground hover:bg-muted hover:text-foreground">
      Trail{call.attempt > 1 ? `, attempt ${call.attempt}` : ''}
    </button>
  )
}

/** One part of an observation. Null: the episode is from before `since`, which did not record it. */
function SawList<T>({ title, items, render, empty = 'None', since = '0.23.0', pulled = false }: { title: string; items: T[] | null | undefined; render: (x: T) => string; empty?: string; since?: string; pulled?: boolean }) {
  return (
    <div>
      <p className="text-muted-foreground">{title}</p>
      {items == null ? (
        <p className="text-muted-foreground italic">
          {pulled ? 'Not pushed: observation mode pull. The agent reads with its tools. See the Trail button.' : `Not recorded (episode before ${since})`}
        </p>
      ) : items.length === 0 ? (
        <p className="text-muted-foreground">{empty}</p>
      ) : (
        <ul className="space-y-0.5">
          {items.map((x, i) => (
            <li key={i} className="whitespace-pre-wrap">
              {render(x)}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

/**
 * What the agent's calls in round t got (0.23.0 Observation): news so far, the recent messages with the masks applied,
 * its own last rounds. God view only: the messages hold secret-channel text and the news can be restricted.
 */
function SawFold({ state, agentId, t, onTrail }: { state: RunState; agentId: string; t: number; onTrail: OnTrail }) {
  const calls = observationsAt(state, agentId, t)
  // The llm_calls of an observed call: the same agent, round and purpose. A retried call has one per attempt.
  const llm = state.events.filter((x): x is EventOf<'llm_call'> => x.type === 'llm_call' && x.agent_id === agentId && x.t === t)
  const isolated = !!state.header?.c3_isolation
  // A pull call is given only the round and its last result (View._for_mode), so its other fields are null on purpose.
  const pulled = state.header?.observation_mode === 'pull'
  return (
    <details className="border-t px-3 py-1.5 text-xs">
      <summary className="cursor-pointer text-muted-foreground">
        What {agentId} saw at R{t}
      </summary>
      {calls.length === 0 && <p className="mt-1 text-muted-foreground">No call in this round.</p>}
      {calls.map((e) => (
        <div key={e.seq} className="mt-1.5 space-y-1">
          <p className="flex flex-wrap items-center gap-1.5 font-medium">
            {PURPOSE_TEXT[e.purpose]}
            {onTrail && llm.filter((c) => c.purpose === e.purpose).map((c) => <TrailButton key={c.seq} call={c} onTrail={onTrail} />)}
          </p>
          {e.observation.mask_noop && <p className="text-amber-700 dark:text-amber-400">The shuffle mask changed nothing in this call: the other sellers' messages have fewer than 2 different texts.</p>}
          <SawList title="News" pulled={pulled} items={e.observation.news} render={(n) => `R${n.t}: ${n.text}`} />
          <SawList
            title="Messages (all channels)"
            pulled={pulled}
            items={e.observation.recent_messages}
            empty={e.purpose === 'act' && isolated ? 'No messages: the pricing call does not see the channel.' : 'None'}
            render={(m) => `R${m.t}, ${m.sender === agentId ? 'you' : m.sender} in ${m.channel_id}: ${m.text}`}
          />
          <SawList
            title="Rival prices"
            since="0.31.0"
            pulled={pulled}
            items={e.observation.rival_history == null ? null : Object.entries(e.observation.rival_history).filter(([, rows]) => rows.length > 0)}
            empty={e.observation.rival_history && Object.keys(e.observation.rival_history).length === 0 ? 'None: the seller has no get_rival_prices grant.' : 'None'}
            render={([id, rows]) => `${id}: ${rows.map((r) => `R${r.t} ${n2(r.price)}`).join(', ')}`}
          />
          <SawList
            title="Own rounds"
            pulled={pulled}
            items={e.observation.own_history}
            render={(r) => `R${r.t}: price ${n2(r.price)}, sold ${n2(r.quantity)}, profit ${n2(r.profit)}${r.set_by_system ? ' (set by the system)' : ''}`}
          />
        </div>
      ))}
    </details>
  )
}

/** `final` is the last settled round of a run that has ended, with how it ended (`finalRound`). Null while the run goes on. */
export function AgentCard({ state, agentId, cursorT, publicView, onTrail, final = null }: { state: RunState; agentId: string; cursorT: number; publicView: boolean; onTrail?: OnTrail; final?: Final | null }) {
  const agent = state.header?.agents.find((a) => a.id === agentId)
  const items = inbox(state, agentId, cursorT)
  const isolated = !!state.header?.c3_isolation
  const r = agentRound(state, agentId, cursorT, publicView)
  const status = r.thinking ? 'thinking' : r.action ? ACTION_TEXT[r.action] : 'no action'
  const maskNoop = !publicView && observationsAt(state, agentId, cursorT).some((e) => e.observation.mask_noop)
  return (
    <Card
      label={`Agent ${agentId}`}
      title={
        <>
          <span className="inline-block size-2.5 rounded-full" style={{ background: sellerColor(state.sellers, agentId) }} />
          <span>Seller {agentId}</span>
          <span className="font-mono text-xs font-normal text-muted-foreground">
            {agent?.policy_kind}
            {agent?.model ? ` · ${agent.model}` : ''}
          </span>
        </>
      }
      footer={
        <span className="flex flex-wrap gap-x-3">
          <span>
            R{cursorT}
            {final && cursorT > final.t ? ' (incomplete)' : ''} price <span className="font-medium text-foreground tabular-nums">{r.price ?? '–'}</span>
          </span>
          <span>{status}</span>
          <span>{r.calls} calls</span>
          {maskNoop && <span title="The other sellers' messages had fewer than 2 different texts, so the shuffle changed nothing (Observation.mask_noop)">shuffle had no effect</span>}
        </span>
      }>
      {publicView ? (
        <p className="p-3 text-xs text-muted-foreground">Public view: the secret-channel inbox and what the agent saw are hidden.</p>
      ) : items.length === 0 ? (
        <p className="p-3 text-xs text-muted-foreground">
          {noTalkTurn(state, agentId) ? `Seller ${agentId} has no talk turn, so it sees no channel messages.` : `No secret-channel messages up to R${cursorT}.`}
        </p>
      ) : (
        <FollowList count={items.length} label={`Inbox of ${agentId}`}>
          {items.map((m) => (
            <li key={m.seq} className={cn('rounded-md px-2 py-1 text-sm', m.direction === 'out' ? 'ml-6 bg-muted' : 'mr-6 border', (m.direction === 'in' ? m.deliverT : m.t) === cursorT && 'ring-1 ring-ring')}>
              <div className="text-xs text-muted-foreground">
                {m.direction === 'in'
                  ? `R${m.deliverT} from ${m.agentId}`
                  : m.receivers === null
                    ? `R${m.t}`
                    : m.receivers.length
                      ? `R${m.t} to ${m.receivers.join(', ')}`
                      : undelivered(m, final)}
                {m.receivers === null && <span title={`Channel members: ${m.members.join(', ') || 'unknown'}`}> · receivers not recorded</span>}
                {isolated && <span title="c3_isolation: the pricing call does not see this channel"> · talk call only</span>}
                {m.asReceived && <span title="Shuffle mask: the text this agent saw, from its observed event. A seller's own messages are not shuffled."> · text as received</span>}
              </div>
              {m.text !== null ? (
                <p className="whitespace-pre-wrap">{m.text}</p>
              ) : (
                <p className="text-xs text-muted-foreground italic">{m.pending ? 'Not seen yet: this agent has not observed the round.' : 'The text this agent saw is not recorded.'}</p>
              )}
            </li>
          ))}
        </FollowList>
      )}
      {!publicView && <SawFold state={state} agentId={agentId} t={cursorT} onTrail={onTrail} />}
    </Card>
  )
}

export function HostCard({ state, cursorT, publicView }: { state: RunState; cursorT: number; publicView: boolean }) {
  const hosts = new Set((state.header?.agents ?? []).filter((a) => a.role === 'host').map((a) => a.id))
  const news = state.news.filter((n) => n.t <= cursorT)
  const hidden = publicView ? news.filter((n) => isRestricted(state, n.visible)).length : 0
  const items = [
    ...news.filter((n) => !publicView || !isRestricted(state, n.visible)).map((n) => ({ seq: n.seq, t: n.t, head: `R${n.t} news, seen by ${n.visible.join(', ') || 'nobody'}`, text: n.text })),
    ...state.messages.filter((m) => hosts.has(m.agentId) && m.t <= cursorT && (!publicView || onPublicChannel(state, m.channelId))).map((m) => ({ seq: m.seq, t: m.t, head: `R${m.t} in ${m.channelId}`, text: m.text })),
  ].sort((a, b) => a.seq - b.seq)
  return (
    <Card label="Host" title="Host" footer={hidden > 0 ? `${hidden} restricted news hidden` : undefined}>
      {items.length === 0 ? (
        <p className="p-3 text-xs text-muted-foreground">No host messages or news up to R{cursorT}.</p>
      ) : (
        <FollowList count={items.length} label="Host messages and news">
          {items.map((m) => (
            <li key={m.seq} className={cn('rounded-md border px-2 py-1 text-sm', m.t === cursorT && 'ring-1 ring-ring')}>
              <div className="text-xs text-muted-foreground">{m.head}</div>
              <p className="whitespace-pre-wrap">{m.text}</p>
            </li>
          ))}
        </FollowList>
      )}
    </Card>
  )
}

export function PublicChannel({ state, cursorT }: { state: RunState; cursorT: number }) {
  const items = publicMessages(state, cursorT)
  const on = (state.header?.channels ?? []).some((c) => c.kind === 'public')
  return (
    <Card label="Public Channel" title="Public Channel">
      {items.length === 0 ? (
        <p className="p-3 text-xs text-muted-foreground">{on ? `No public messages up to R${cursorT}.` : 'This run has no public channel.'}</p>
      ) : (
        <FollowList count={items.length} label="Public messages">
          {items.map((m) => (
            <li key={m.seq} className={cn('rounded-md px-2 py-1 text-sm', m.t === cursorT && 'bg-muted')}>
              <div className="text-xs text-muted-foreground">
                R{m.t}{' '}
                <span className="font-semibold" style={{ color: sellerColor(state.sellers, m.agentId) }}>
                  {m.agentId}
                </span>
              </div>
              <p className="whitespace-pre-wrap">{m.text}</p>
            </li>
          ))}
        </FollowList>
      )}
    </Card>
  )
}

type LogFilter = { calls: boolean; agent: string | null; purpose: 'talk' | 'act' | null }
const NO_FILTER: LogFilter = { calls: false, agent: null, purpose: null }

/** A filter chip. It is a toggle: `aria-pressed` tells whether it is on. */
function Chip({ on, onClick, children, label }: { on: boolean; onClick: () => void; children: ReactNode; label?: string }) {
  return (
    <button type="button" aria-pressed={on} aria-label={label} onClick={onClick} className={cn('rounded-full border px-2 py-0.5 text-[11px] hover:bg-muted', on && 'border-foreground bg-muted font-medium')}>
      {children}
    </button>
  )
}

/**
 * The newest events first, in groups under a round header (UX review item 4). The header of the round at the scrubber is marked,
 * and a header folds its rows. The chips filter by model calls, seller and call kind. A click on a row moves the scrubber to
 * the event's round. Public view leaves out private posts and restricted news. With `onTrail` (god view), each model call has a
 * Trail button.
 */
export function EventLog({ state, cursorT, onPickT, publicView, onTrail }: { state: RunState; cursorT: number; onPickT: (t: number) => void; publicView: boolean; onTrail?: OnTrail }) {
  const [filter, setFilter] = useState<LogFilter>(NO_FILTER)
  const [folded, setFolded] = useState<ReadonlySet<number>>(new Set())
  const all = (publicView ? publicEvents(state, state.events) : state.events).flatMap((e) => {
    const text = logLine(e)
    return text === null
      ? []
      : [{ seq: e.seq, t: e.t, text, call: e.type === 'llm_call' ? e : null, agent: 'agent_id' in e ? (e.agent_id as string) : null }]
  })
  const sellers = (state.header?.agents ?? []).filter((a) => a.role === 'seller').map((a) => a.id)
  const keep = (l: (typeof all)[number]) => {
    if ((filter.calls || filter.purpose) && !l.call) return false
    if (filter.agent && l.agent !== filter.agent) return false
    return !filter.purpose || l.call?.purpose === filter.purpose
  }
  const lines = all.filter(keep).reverse().slice(0, 400)
  // One group per round, in the order of first appearance, also when a late event of an earlier round comes after a later one (CR-92 5).
  const byRound = new Map<number, typeof lines>()
  for (const l of lines) byRound.set(l.t, [...(byRound.get(l.t) ?? []), l])
  const rounds = [...byRound].map(([t, rows]) => ({ t, rows }))
  const toggle = (t: number) => setFolded((f) => new Set(f.has(t) ? [...f].filter((x) => x !== t) : [...f, t]))
  const set = (patch: Partial<LogFilter>) => setFilter((f) => ({ ...f, ...patch }))
  const filtered = filter.calls || filter.agent !== null || filter.purpose !== null
  return (
    <Card label="Log" title="Log">
      <div role="group" aria-label="Log filters" className="flex flex-wrap items-center gap-1 border-b p-1.5">
        {!publicView && (
          <Chip on={filter.calls} onClick={() => set({ calls: !filter.calls })}>
            Model calls only
          </Chip>
        )}
        {sellers.map((id) => (
          <Chip key={id} on={filter.agent === id} label={`Seller ${id}`} onClick={() => set({ agent: filter.agent === id ? null : id })}>
            {id}
          </Chip>
        ))}
        {!publicView && (
          <>
            <Chip on={filter.purpose === 'talk'} onClick={() => set({ purpose: filter.purpose === 'talk' ? null : 'talk' })}>
              Talk
            </Chip>
            <Chip on={filter.purpose === 'act'} onClick={() => set({ purpose: filter.purpose === 'act' ? null : 'act' })}>
              Pricing
            </Chip>
          </>
        )}
        {filtered && (
          <button type="button" className="text-[11px] underline underline-offset-2" onClick={() => setFilter(NO_FILTER)}>
            Clear filters
          </button>
        )}
        <button
          type="button"
          className="ml-auto text-[11px] underline underline-offset-2"
          onClick={() => setFolded(folded.size ? new Set() : new Set(rounds.map((r) => r.t).filter((t) => t !== cursorT)))}>
          {folded.size ? 'Open all rounds' : 'Fold other rounds'}
        </button>
      </div>
      {filtered && <p className="px-2 pt-1 text-[11px] text-muted-foreground">{lines.length} of {all.length} events.</p>}
      <ol aria-label="Event log" className="max-h-[32rem] overflow-y-auto p-1 text-xs">
        {lines.length === 0 && <li className="px-2 py-1 text-muted-foreground">No event matches these filters.</li>}
        {rounds.flatMap((g) => {
          const open = !folded.has(g.t)
          return [
            <li key={`round-${g.t}`} className={cn('sticky top-0 bg-background', g.t === cursorT && 'font-medium')} aria-current={g.t === cursorT ? 'true' : undefined}>
              <button type="button" aria-expanded={open} onClick={() => toggle(g.t)} className={cn('flex w-full items-center gap-1 rounded px-1 py-0.5 text-left text-[11px] uppercase tracking-wide text-muted-foreground hover:bg-muted', g.t === cursorT && 'bg-muted text-foreground')}>
                <span aria-hidden>{open ? '▾' : '▸'}</span>
                <span>
                  R{g.t} · {g.rows.length} event{g.rows.length === 1 ? '' : 's'}
                  {g.t === cursorT ? ' · at the scrubber' : ''}
                </span>
              </button>
            </li>,
            ...(open
              ? g.rows.map((l) => (
                  <li key={l.seq} className="flex items-start gap-1">
                    <button type="button" onClick={() => onPickT(l.t)} className={cn('flex w-full gap-2 rounded px-2 py-0.5 text-left hover:bg-muted', l.t === cursorT && 'bg-muted')}>
                      <span className="w-8 shrink-0 tabular-nums text-muted-foreground">R{l.t}</span>
                      <span className="min-w-0 break-words">{l.text}</span>
                    </button>
                    <span className="w-[4.5rem] shrink-0">{onTrail && l.call && <TrailButton call={l.call} onTrail={onTrail} />}</span>
                  </li>
                ))
              : []),
          ]
        })}
      </ol>
    </Card>
  )
}

/** The Setting panel of a started run: one read-only line. */
export function SettingSummary({ run, condition, state }: { run: RunDetail | undefined; condition: string | null; state: RunState }) {
  if (!run) return null
  const limits = run.config.limits
  const parts: [string, string][] = [
    [condition ? `condition ${condition}` : run.kind, condition ? 'The condition of the study design that this run follows.' : 'The kind of the run.'],
    [`seed ${run.seed}`, 'The seed of the random draws. The same seed gives the same shocks.'],
    [`split ${run.split}`, 'dev seeds tune the rule. test seeds run once, after the freeze.'],
    [`rounds ${state.settledT ?? 0} of ${run.max_rounds ?? '?'}`, 'Rounds that are settled, of the rounds that the run has.'],
    [`scenario ${run.scenario_id}`, 'The scenario that holds the market rules.'],
  ]
  if (limits) parts.push([`limits ${Object.entries(limits).map(([k, v]) => `${k} ${v}`).join(', ')}`, 'The caps of the run. The run stops when one is reached.'])
  return (
    <p aria-label="Setting" className="flex flex-wrap items-center gap-x-1.5 gap-y-1 rounded-lg border px-3 py-2 text-xs text-muted-foreground">
      <span className="font-medium text-foreground">Setting</span>
      {parts.map(([text, why]) => (
        <span key={text}>
          {' · '}
          <span title={why} className="rounded bg-muted px-1.5 py-0.5">
            {text}
          </span>
        </span>
      ))}
    </p>
  )
}
