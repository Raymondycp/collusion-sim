import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { setupServer } from 'msw/node'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { handlers, wsHandlers } from '@/mocks/handlers'
import { makeEpisode } from '@/mocks/episode'
import { mockRuns } from '@/mocks/data'
import type { EpisodeHeader, SimEvent } from '@/api/types'
import { foldEvents, type RunState } from '@/run/fold'
import { storeFor, type RunStream } from '@/run/useRunStream'
import type { Marker } from '@/run/fold'
import { agentRound, finalRound, inbox, panelRound, pricePairs, publicEvents, publicMarkers, publicMessages } from '@/features/workbench/derive'
import { AgentCard, EventLog, HostCard } from '@/features/workbench/Panels'
import { WorkbenchPage } from '@/features/workbench/WorkbenchPage'
import { atCursor, MetricsBar } from '@/features/workbench/MetricsBar'
import { RivalChart } from '@/features/workbench/RivalChart'

// The page test reads the markers that reach the chart and the timeline. jsdom has no canvas.
const seen = vi.hoisted(() => ({ chart: [] as Marker[][], timeline: [] as Marker[][], slider: [] as { t: number | null; maxT: number }[] }))
vi.mock('lightweight-charts', () => {
  const any: unknown = new Proxy(() => any, { get: (_, k) => (k === 'then' ? undefined : any), apply: () => any })
  return { ColorType: {}, LineStyle: {}, LineType: {}, LineSeries: 'Line', HistogramSeries: 'Histogram', createSeriesMarkers: () => any, createChart: () => any }
})
vi.mock('@/components/chart/PriceChart', async (orig) => ({
  ...(await orig<typeof import('@/components/chart/PriceChart')>()),
  PriceChart: ({ state }: { state: RunState }) => (seen.chart.push(state.markers), null),
}))
vi.mock('@/features/run-view/TimelineBar', () => ({
  TimelineBar: ({ markers, t, maxT }: { markers: Marker[]; t: number | null; maxT: number }) => (seen.timeline.push(markers), seen.slider.push({ t, maxT }), null),
}))

const server = setupServer(...handlers, ...wsHandlers)
beforeAll(() => {
  server.listen({ onUnhandledFrame: 'bypass' })
})
afterEach(() => {
  cleanup()
  server.resetHandlers()
})
afterAll(() => server.close())

const chat = foldEvents(makeEpisode({ runId: 'run_WB', kind: 'exploratory', seed: 1, rounds: 30, start: '2026-10-23T09:00:00Z', conditionId: null, branch: null }))
const priv = chat.messages.find((m) => m.channelId !== 'public')!

/** A three-seller state where C3-style isolation gives one private channel to A and B only. */
function threeSellers(receivers?: string[] | null): RunState {
  const header = { ...chat.header!, agents: ['A', 'B', 'C'].map((id) => ({ ...chat.header!.agents[0]!, id })), channels: [
    { id: 'public', kind: 'public', participants: ['A', 'B', 'C'] },
    { id: 'private_A_B', kind: 'private', participants: ['A', 'B'] },
  ] } as EpisodeHeader
  const ev = (e: Record<string, unknown>, seq: number) => ({ run_id: 'run_3', wall_time: '2026-10-23T09:00:00Z', seq, ...e }) as unknown as SimEvent
  return foldEvents([
    ev({ type: 'run_started', t: 0, header }, 0),
    ev({ type: 'posted', t: 1, deliver_t: 2, agent_id: 'A', channel_id: 'private_A_B', text: 'hold at 9', tier: 'A', ...(receivers !== undefined ? { receivers } : {}) }, 1),
    ev({ type: 'settled', t: 1, prices: { A: 6, B: 8, C: 10 } }, 2),
  ])
}

describe('workbench derive', () => {
  it('an incoming secret message shows from its delivery round, the sender sees it at once', () => {
    const [sender, other] = [priv.agentId, chat.sellers.find((s) => s !== priv.agentId)!]
    expect(priv.deliverT).toBeGreaterThan(priv.t)
    expect(inbox(chat, sender, priv.t).find((m) => m.seq === priv.seq)?.direction).toBe('out')
    expect(inbox(chat, other, priv.t).some((m) => m.seq === priv.seq)).toBe(false)
    expect(inbox(chat, other, priv.deliverT).find((m) => m.seq === priv.seq)?.direction).toBe('in')
  })

  it('a post without receivers (before 0.21.0) falls back to the channel members; a seller outside the channel gets nothing', () => {
    const s = threeSellers()
    expect(inbox(s, 'A', 5)[0]).toMatchObject({ direction: 'out', receivers: null, members: ['B'] })
    expect(inbox(s, 'B', 5)[0]).toMatchObject({ direction: 'in', agentId: 'A' })
    expect(inbox(s, 'C', 5)).toEqual([])
  })

  it('receivers come from posted.receivers: a member not in the list gets nothing, [] reaches nobody (CR-50)', () => {
    expect(inbox(threeSellers(['B']), 'A', 5)[0]).toMatchObject({ direction: 'out', receivers: ['B'] })
    expect(inbox(threeSellers(['B']), 'B', 5)[0]).toMatchObject({ direction: 'in', agentId: 'A', text: 'hold at 9', asReceived: false })
    expect(inbox(threeSellers([]), 'B', 5)).toEqual([])
    expect(inbox(threeSellers([]), 'A', 5)[0]).toMatchObject({ direction: 'out', receivers: [] })
  })

  it('public messages leave out private posts and rounds after the cursor', () => {
    const pub = publicMessages(chat, 30)
    expect(pub.length).toBeGreaterThan(0)
    expect(pub.every((m) => m.channelId === 'public')).toBe(true)
    expect(publicMessages(chat, pub[0]!.t - 1).some((m) => m.seq === pub[0]!.seq)).toBe(false)
  })

  it('public markers drop private posts and keep public posts and other markers', () => {
    const kept = publicMarkers(chat, chat.markers)
    expect(kept.some((m) => m.kind === 'post' && m.channelId !== 'public')).toBe(false)
    expect(kept.some((m) => m.kind === 'post' && m.channelId === 'public')).toBe(true)
    expect(kept.filter((m) => m.kind !== 'post')).toEqual(chat.markers.filter((m) => m.kind !== 'post'))
    expect(chat.markers.some((m) => m.kind === 'post' && m.channelId !== 'public')).toBe(true)
  })

  it('the rival price is the mean of the other sellers', () => {
    expect(pricePairs(threeSellers(), 'A')).toEqual([{ t: 1, own: 6, rival: 9 }])
  })

  it('the agent footer gives the settled price and counts calls up to the round', () => {
    const r = agentRound(chat, 'A', 5)
    expect(r.price).toBe(chat.prices.A!.find((p) => p.t === 5)!.value)
    expect(r.calls).toBe(chat.events.filter((e) => e.type === 'llm_call' && e.agent_id === 'A' && e.t <= 5).length)
    expect(r.thinking).toBe(false)
  })
})

describe('inbox from the record (UI-WORKBENCH.md §6 step 5, CR-50)', () => {
  const child = (mode: 'shuffle' | 'clear') =>
    foldEvents(makeEpisode({ runId: `run_${mode}`, kind: 'research', seed: 101, rounds: 6, start: '2026-10-23T09:00:00Z', conditionId: 'C1', branch: { parentRunId: 'run_MOCK01', forkT: 20, intervention: { type: 'ablation', channel_id: 'private_A_B', mode }, sampleIndex: 0 } }))

  it('in a shuffle child, an incoming line shows the text from the receiver\'s observed event, not the posted text', () => {
    const s = child('shuffle')
    const post = s.messages.find((m) => m.t === 21 && m.agentId === 'A')!
    const seen = s.events.find((e) => e.type === 'observed' && e.agent_id === 'B' && e.t === 22)
    const seenText = seen?.type === 'observed' ? seen.observation.new_messages?.[0]?.text : undefined
    expect(seenText).toBeTruthy()
    expect(seenText).not.toBe(post.text)
    expect(inbox(s, 'B', 22).find((m) => m.seq === post.seq)).toMatchObject({ direction: 'in', text: seenText, asReceived: true })
    expect(inbox(s, 'A', 22).find((m) => m.seq === post.seq)).toMatchObject({ direction: 'out', text: post.text, receivers: ['B'] })
    render(<AgentCard state={s} agentId="B" cursorT={22} publicView={false} />)
    expect(screen.getByText(seenText!)).toBeInTheDocument()
    expect(screen.queryByText(post.text)).toBeNull()
    expect(screen.getByText(/text as received/)).toBeInTheDocument()
  })

  it('a shuffle child with no observed new_messages (before 0.18.0) says the text is not recorded', () => {
    const s = child('shuffle')
    const old = { ...s, events: s.events.map((e) => (e.type === 'observed' ? { ...e, observation: { ...e.observation, new_messages: null } } : e)) }
    render(<AgentCard state={old} agentId="B" cursorT={22} publicView={false} />)
    expect(screen.getByText('The text this agent saw is not recorded.')).toBeInTheDocument()
  })

  /** A shuffle child where A posts `texts` in round 1 on private_A_B; B observes round 2 with `seen` (none: no event yet). */
  function twoPosts(texts: string[], seen: string[] | null | 'none', ended = false): RunState {
    const header = { ...chat.header!, branch: { parent_run_id: 'run_P', fork_t: 1, sample_index: 0, intervention: { type: 'ablation', channel_id: 'private_A_B', mode: 'shuffle' } } } as EpisodeHeader
    const own = { last_price: null, last_quantity: null, last_profit: null }
    const events: Record<string, unknown>[] = [
      { type: 'run_started', t: 0, header },
      { type: 'run_status_changed', t: 0, status: 'running', reason: null },
      ...texts.map((text) => ({ type: 'posted', t: 1, deliver_t: 2, agent_id: 'A', channel_id: 'private_A_B', text, receivers: ['B'] })),
      { type: 'round_started', t: 2, round: 2, reference: {} },
    ]
    if (seen !== 'none') {
      const new_messages = seen && seen.map((text) => ({ channel_id: 'private_A_B', sender: 'A', text }))
      events.push({ type: 'observed', t: 2, agent_id: 'B', purpose: 'talk', observation: { t: 2, own, new_message_count: seen?.length ?? 0, headlines: [], masked: [], new_messages } })
    }
    if (ended) events.push({ type: 'run_ended', t: 2, status: 'stopped', end_reason: 'stopped_by_user', episode_hash: null, cost_usd: 0, error: null })
    // The fold stops at a gap in seq, so the events are numbered without one.
    return foldEvents(events.map((e, seq) => ({ run_id: 'run_S', wall_time: '2026-10-23T09:00:00Z', tier: 'A', seq, ...e }) as unknown as SimEvent))
  }

  it('two posts in one round: each incoming line gets its own observed entry, in post order (CR-57 4)', () => {
    const s = twoPosts(['first', 'second'], ['seen as second', 'seen as first'])
    expect(inbox(s, 'B', 2).map((m) => m.text)).toEqual(['seen as second', 'seen as first'])
  })

  it('a live shuffle child says "not seen yet" until the receiver observes the round; an ended run says "not recorded" (CR-57 2)', () => {
    expect(inbox(twoPosts(['hi'], 'none'), 'B', 2)[0]).toMatchObject({ direction: 'in', text: null, pending: true })
    render(<AgentCard state={twoPosts(['hi'], 'none')} agentId="B" cursorT={2} publicView={false} />)
    expect(screen.getByText('Not seen yet: this agent has not observed the round.')).toBeInTheDocument()
    cleanup()
    render(<AgentCard state={twoPosts(['hi'], 'none', true)} agentId="B" cursorT={2} publicView={false} />)
    expect(screen.getByText('The text this agent saw is not recorded.')).toBeInTheDocument()
    expect(inbox(twoPosts(['hi'], null), 'B', 2)[0]).toMatchObject({ text: null, pending: false })
  })

  it('the observed event decides the delivery: a clear-mask post with null receivers is not incoming (CR-57 3)', () => {
    const events = makeEpisode({ runId: 'run_old', kind: 'research', seed: 101, rounds: 6, start: '2026-10-23T09:00:00Z', conditionId: 'C1', branch: { parentRunId: 'run_MOCK01', forkT: 20, intervention: { type: 'ablation', channel_id: 'private_A_B', mode: 'clear' }, sampleIndex: 0 } })
    const s = foldEvents(events.map((e) => (e.type === 'posted' ? { ...e, receivers: null } : e)))
    const post = s.messages.find((m) => m.t === 21 && m.agentId === 'A')!
    expect(post.receivers).toBeNull()
    expect(inbox(s, 'B', 25).some((m) => m.seq === post.seq)).toBe(false)
    // Without observed new_messages the members decide, as before.
    const old = { ...s, events: s.events.map((e) => (e.type === 'observed' ? { ...e, observation: { ...e.observation, new_messages: null } } : e)) }
    expect(inbox(old, 'B', 25).some((m) => m.seq === post.seq)).toBe(true)
  })

  it('outside a shuffle child the incoming text is the posted text', () => {
    const post = chat.messages.find((m) => m.channelId !== 'public' && m.agentId === 'A')!
    expect(inbox(chat, 'B', post.deliverT).find((m) => m.seq === post.seq)).toMatchObject({ text: post.text, asReceived: false, receivers: ['B'] })
  })

  it('a clear child: the post has receivers [] and no inbox gets it; the sender sees the mask as the cause (CR-67 1)', () => {
    const s = child('clear')
    const post = s.messages.find((m) => m.t === 21 && m.agentId === 'A')!
    expect(post.receivers).toEqual([])
    expect(inbox(s, 'B', 25).some((m) => m.seq === post.seq)).toBe(false)
    render(<AgentCard state={s} agentId="A" cursorT={22} publicView={false} />)
    expect(screen.getByText(/^R21, not delivered: the clear mask removes this channel$/)).toBeInTheDocument()
  })
})

describe('workbench live mode', () => {
  // A live run cut in round 6: the round started, A acted, B did not act yet.
  const all = makeEpisode({ runId: 'run_LIVE', kind: 'exploratory', seed: 1, rounds: 30, start: '2026-10-23T09:00:00Z', conditionId: null, branch: null })
  const start6 = all.findIndex((e) => e.type === 'round_started' && e.t === 6)
  const actA = all.findIndex((e, i) => i > start6 && e.type === 'acted' && e.agent_id === 'A')
  const live = foldEvents(all.slice(0, actA + 1).filter((e) => !(e.type === 'acted' && e.agent_id === 'B' && e.t === 6)))

  it('following a live run, the panels show the round in progress; a picked round stays settled', () => {
    expect(live.status).toBe('running')
    expect(live.settledT).toBe(5)
    expect(panelRound(live, null)).toBe(6)
    expect(panelRound(live, 3)).toBe(3)
    expect(panelRound(live, 9)).toBe(5)
    expect(panelRound(chat, null)).toBe(chat.settledT)
  })

  it('in the round in progress, an agent that acted says so and the other is thinking', () => {
    expect(agentRound(live, 'A', 6)).toMatchObject({ action: expect.any(String), thinking: false })
    expect(agentRound(live, 'B', 6)).toMatchObject({ action: null, thinking: true, price: null })
    render(<AgentCard state={live} agentId="B" cursorT={6} publicView={false} />)
    expect(screen.getByText('thinking')).toBeInTheDocument()
  })

  it('a stopped or paused run is not thinking', () => {
    expect(agentRound({ ...live, status: 'paused' }, 'B', 6).thinking).toBe(false)
  })
})

describe('workbench panels', () => {
  it('public view hides the secret inbox text', () => {
    const { rerender } = render(<AgentCard state={chat} agentId={priv.agentId} cursorT={30} publicView={false} />)
    expect(screen.getAllByText(priv.text).length).toBeGreaterThan(0)
    rerender(<AgentCard state={chat} agentId={priv.agentId} cursorT={30} publicView />)
    expect(screen.queryByText(priv.text)).toBeNull()
    expect(screen.getByText(/secret-channel inbox and what the agent saw are hidden/)).toBeInTheDocument()
  })

  it('a log click moves the scrubber to the round; public view leaves out private posts', () => {
    const onPickT = vi.fn()
    const { rerender } = render(<EventLog state={chat} cursorT={30} onPickT={onPickT} publicView={false} />)
    const line = `${priv.agentId} posted in ${priv.channelId}`
    fireEvent.click(screen.getAllByText(line)[0]!)
    expect(onPickT).toHaveBeenCalledWith(expect.any(Number))
    rerender(<EventLog state={chat} cursorT={30} onPickT={onPickT} publicView />)
    expect(screen.queryByText(line)).toBeNull()
    expect(screen.getAllByText(/posted in public/).length).toBeGreaterThan(0)
  })

  it('market chart 2 draws one dot per settled round up to the cursor, and the reference marks', () => {
    const { container } = render(<RivalChart state={chat} cursorT={12} />)
    const dots = container.querySelectorAll('circle[data-t]')
    expect(dots.length).toBe(chat.prices.A!.filter((p) => p.t <= 12).length)
    expect(screen.getByLabelText(/^Nash /)).toBeInTheDocument()
    expect(screen.getByLabelText(/^Joint optimum /)).toBeInTheDocument()
  })
})

describe('workbench metrics bar', () => {
  const run = mockRuns.find((r) => r.header.run_id === 'run_MOCK01')!
  const stream: RunStream = { state: foldEvents(run.events), phase: 'ended', detail: null, indicators: {}, indicatorError: null }

  function renderBar(experimentId: string | null = null, publicView = false) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <MetricsBar runId="run_MOCK01" stream={stream} status="completed" cursorT={10} experimentId={experimentId} evaluationId={null} publicView={publicView} split={null} />
      </QueryClientProvider>,
    )
  }
  it('without an all-seller output, a value combines the per-pair outputs: largest for corr_lag, else the mean', () => {
    const pairs = { 'corr_lag.A>B': [0.1, 0.2, null], 'corr_lag.B>A': [0.6, null, null] }
    expect(atCursor('corr_lag', pairs)).toBe(0.6)
    expect(atCursor('corr_raw', { 'corr_raw.A|B': [0.2], 'corr_raw.A|C': [0.4] })).toBeCloseTo(0.3)
    expect(atCursor('delta_p', { 'delta_p.A': [0.9], delta_p: [0.5] })).toBe(0.5)
    expect(atCursor('msg_volume', { msg_volume: [null] })).toBeNull()
  })

  const cell = (id: string) => document.querySelector(`[data-metric="${id}"]`)!

  it('shows the three evidence groups in order, with designed items greyed and no combined score', async () => {
    renderBar()
    expect(screen.getAllByRole('group').map((g) => g.getAttribute('aria-label'))).toEqual(['Conversation', 'Coalition advantage', 'Dependence'])
    expect(within(screen.getByRole('group', { name: 'Conversation' })).getByText('Say–do agreement')).toBeInTheDocument()
    expect(screen.getAllByText('designed, not built')).toHaveLength(5)
  })

  it('indicator values are at the cursor; feature values say why they are missing', async () => {
    renderBar()
    await waitFor(() => expect(cell('delta_p').textContent).toMatch(/^-?\d+\.\d{3}$/))
    expect(cell('corr_cond')).toHaveTextContent('not on this server')
    expect(cell('llm_agreement')).toHaveTextContent('not judged')
    await waitFor(() => expect(cell('ablation_effect')).toHaveTextContent('no branch'))
    expect(cell('punishment_depth')).toHaveTextContent('no experiment')
  })

  it('a value that is measured and a placeholder never share a list: the placeholder is a dash with its reason (UX review item 8)', async () => {
    renderBar()
    await waitFor(() => expect(cell('delta_p').textContent).toMatch(/^-?\d+\.\d{3}$/))
    const group = screen.getByRole('group', { name: 'Coalition advantage' })
    const measured = within(group).getByLabelText('Coalition advantage: measured')
    const missing = within(group).getByLabelText('Coalition advantage: not available')
    expect(measured).toContainElement(cell('delta_p') as HTMLElement)
    expect(missing).not.toContainElement(cell('delta_p') as HTMLElement)
    expect(measured.querySelectorAll('[data-unavailable]')).toHaveLength(0)
    await waitFor(() => expect(cell('ablation_effect')).toHaveTextContent('no branch'))
    expect(within(screen.getByRole('group', { name: 'Dependence' })).getByLabelText('Dependence: not available')).toContainElement(cell('ablation_effect') as HTMLElement)
    expect(missing).not.toContainElement(cell('ablation_effect') as HTMLElement)
    expect(cell('ablation_effect')).toHaveAttribute('data-unavailable', 'true')
    expect(cell('ablation_effect').className).toContain("before:content-['—']")
    expect(within(group).getByText('Not available')).toBeInTheDocument()
    // Every row of the not-available list has a reason, and no bare number.
    for (const dd of missing.querySelectorAll('dd')) expect(dd.textContent).toMatch(/[a-z]/i)
    expect(screen.queryAllByText('0').filter((el) => el.hasAttribute('data-unavailable'))).toHaveLength(0)
  })

  it('a run that is live and in a locked split has no measured list at all', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <MetricsBar runId="run_MOCK01" stream={{ ...stream, state: { ...stream.state, status: 'running' } }} status="running" cursorT={10} experimentId={null} evaluationId={null} publicView={false} split="dev" />
      </QueryClientProvider>,
    )
    expect(screen.queryAllByLabelText(/: measured$/)).toHaveLength(0)
    expect(document.querySelectorAll('[data-metric]')).not.toHaveLength(0)
    for (const el of document.querySelectorAll('[data-metric]')) {
      expect(el.textContent).toBe('after the run ends')
      expect(el).toHaveAttribute('data-unavailable', 'true')
    }
  })
})

describe('workbench public view rules (CR-48)', () => {
  /** Two sellers and a host. News at R1 for A only and at R2 for both; host posts on the private and the public channel. */
  function restricted(c3 = false): RunState {
    const header = {
      ...chat.header!,
      c3_isolation: c3,
      agents: [...chat.header!.agents, { ...chat.header!.agents[0]!, id: 'H', role: 'host' }],
    } as EpisodeHeader
    const ev = (e: Record<string, unknown>, seq: number) => ({ run_id: 'run_N', wall_time: '2026-10-23T09:00:00Z', seq, ...e }) as unknown as SimEvent
    return foldEvents([
      ev({ type: 'run_started', t: 0, header }, 0),
      ev({ type: 'news_published', t: 1, tier: 'A', event_index: 0, text: 'only A knows', visible: ['A'] }, 1),
      ev({ type: 'news_published', t: 2, tier: 'A', event_index: 1, text: 'all know', visible: ['A', 'B'] }, 2),
      ev({ type: 'posted', t: 2, deliver_t: 3, agent_id: 'H', channel_id: 'private_A_B', text: 'host secret', tier: 'A' }, 3),
      ev({ type: 'posted', t: 2, deliver_t: 3, agent_id: 'H', channel_id: 'public', text: 'host public', tier: 'A' }, 4),
      ev({ type: 'posted', t: 2, deliver_t: 3, agent_id: 'A', channel_id: 'private_A_B', text: 'hold at 9', tier: 'A' }, 5),
    ])
  }

  it('restricted news leaves the public markers and events; news for all stays', () => {
    const s = restricted()
    expect(s.markers.filter((m) => m.kind === 'news')).toHaveLength(2)
    expect(publicMarkers(s, s.markers).filter((m) => m.kind === 'news').map((m) => m.text)).toEqual(['all know'])
    expect(publicEvents(s, s.events).filter((e) => e.type === 'news_published').map((e) => e.seq)).toEqual([2])
  })

  it('the Host card in public view hides restricted news and private host posts, and counts the hidden news', () => {
    const s = restricted()
    const { rerender } = render(<HostCard state={s} cursorT={5} publicView={false} />)
    for (const text of ['only A knows', 'all know', 'host secret', 'host public']) expect(screen.getByText(text)).toBeInTheDocument()
    rerender(<HostCard state={s} cursorT={5} publicView />)
    expect(screen.queryByText('only A knows')).toBeNull()
    expect(screen.queryByText('host secret')).toBeNull()
    expect(screen.getByText('all know')).toBeInTheDocument()
    expect(screen.getByText('host public')).toBeInTheDocument()
    expect(screen.getByText('1 restricted news hidden')).toBeInTheDocument()
  })

  it('the log in public view has no line for restricted news', () => {
    const s = restricted()
    const { rerender } = render(<EventLog state={s} cursorT={5} onPickT={vi.fn()} publicView={false} />)
    expect(screen.getByText('News: only A knows')).toBeInTheDocument()
    rerender(<EventLog state={s} cursorT={5} onPickT={vi.fn()} publicView />)
    expect(screen.queryByText('News: only A knows')).toBeNull()
    expect(screen.getByText('News: all know')).toBeInTheDocument()
  })

  it('under c3_isolation each inbox message says "talk call only"; a post with no receivers says "receivers not recorded"', () => {
    render(<AgentCard state={restricted(true)} agentId="A" cursorT={5} publicView={false} />)
    const heads = () => screen.getAllByText(/receivers not recorded/).map((e) => e.parentElement!)
    expect(heads()).toHaveLength(2)
    expect(heads().every((h) => h.textContent!.includes('talk call only'))).toBe(true)
    expect(screen.getByTitle('Channel members: B')).toBeInTheDocument()
    cleanup()
    render(<AgentCard state={restricted(false)} agentId="A" cursorT={5} publicView={false} />)
    expect(heads().some((h) => h.textContent!.includes('talk call only'))).toBe(false)
  })
})

describe('workbench page, both views (CR-48 1)', () => {
  const run = mockRuns.find((r) => r.header.run_id === 'run_MOCK01')!
  const privPost = (ms: Marker[]) => ms.some((m) => m.kind === 'post' && m.channelId !== 'public')
  const restrictedNews = (ms: Marker[]) => ms.some((m) => m.kind === 'news' && m.text === 'restricted for the test')
  let privText = ''

  beforeAll(() => {
    // One news item that only seller A sees, so the page has restricted news to hide.
    const i = run.events.findIndex((e) => e.type === 'news_published')
    run.events[i] = { ...run.events[i]!, text: 'restricted for the test', visible: ['A'] } as SimEvent
    privText = foldEvents(run.events).messages.find((m) => m.channelId !== 'public')!.text
  })

  async function renderPage(search: string) {
    seen.chart.length = 0
    seen.timeline.length = 0
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const router = createMemoryRouter([{ path: '/workbench/:runId', element: <WorkbenchPage /> }], { initialEntries: [`/workbench/run_MOCK01${search}`] })
    render(
      <QueryClientProvider client={qc}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )
    await screen.findAllByText(/Run ended: completed/)
  }

  it('without ?experiment=, the metrics read the feature table of the run\'s own experiment (A7 finding 6)', async () => {
    const asked: string[] = []
    const listen = ({ request }: { request: Request }) => void asked.push(new URL(request.url).pathname)
    server.events.on('request:start', listen)
    const run = mockRuns.find((r) => r.id === 'run_MOCK01')!
    run.experimentId = 'exp_OWN'
    try {
      await renderPage('')
      await waitFor(() => expect(asked).toContain('/api/experiments/exp_OWN/features'))
      expect(screen.queryByText(/not in an experiment/)).toBeNull()
    } finally {
      run.experimentId = undefined
      server.events.removeListener('request:start', listen)
    }
  })

  it('god view passes private posts and restricted news to the chart, the timeline, the log and the inbox', async () => {
    await renderPage('')
    expect(privPost(seen.chart.at(-1)!)).toBe(true)
    expect(privPost(seen.timeline.at(-1)!)).toBe(true)
    expect(restrictedNews(seen.timeline.at(-1)!)).toBe(true)
    expect(screen.getAllByText(/posted in private/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(privText).length).toBeGreaterThan(0)
    expect(screen.getAllByText('restricted for the test').length).toBeGreaterThan(0)
    expect(within(screen.getByRole('group', { name: 'Conversation' })).queryByText('hidden in public view')).toBeNull()
  })

  it('public view passes none of them, hides the inboxes and greys the Conversation group', async () => {
    await renderPage('?view=public')
    expect(seen.chart.length).toBeGreaterThan(0)
    expect(seen.chart.some(privPost)).toBe(false)
    expect(seen.timeline.some(privPost)).toBe(false)
    expect(seen.timeline.some(restrictedNews)).toBe(false)
    expect(screen.queryByText(/posted in private/)).toBeNull()
    expect(screen.queryByText(privText)).toBeNull()
    expect(screen.queryByText(/restricted for the test/)).toBeNull()
    expect(screen.getByText('1 restricted news hidden')).toBeInTheDocument()
    expect(screen.getAllByText(/secret-channel inbox and what the agent saw are hidden/)).toHaveLength(2)
    expect(within(screen.getByRole('group', { name: 'Conversation' })).getAllByText('hidden in public view')).toHaveLength(3)
    expect(within(screen.getByRole('group', { name: 'Coalition advantage' })).queryByText('hidden in public view')).toBeNull()
  })
})

describe('dev and test runs hide metrics while live (CR-49 2)', () => {
  const run = mockRuns.find((r) => r.header.run_id === 'run_MOCK01')!
  const stream: RunStream = { state: foldEvents(run.events), phase: 'ended', detail: null, indicators: {}, indicatorError: null }
  const live: RunStream = { ...stream, phase: 'live', state: { ...stream.state, status: 'running' } }
  const indicatorCalls: string[] = []
  // The two locked-run tests use a run id that no other test requests. The request log is global, so a request that an earlier test
  // starts late (after its cleanup) would land in this test's window under load. A run id of its own keeps those out (CR flaky 2).
  const LOCKED_ID = 'run_LOCKED'
  const lockedCalls = () => indicatorCalls.filter((u) => u.includes(`/runs/${LOCKED_ID}/`))
  beforeAll(() => {
    server.events.on('request:start', ({ request }) => void (new URL(request.url).pathname.includes('/indicators/') && indicatorCalls.push(request.url)))
  })

  function renderBar(split: string | undefined, s: RunStream, status: 'completed' | 'running', id = 'run_MOCK01') {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <MetricsBar runId={id} stream={s} status={status} cursorT={10} experimentId="exp_X" evaluationId={null} publicView={false} split={split} />
      </QueryClientProvider>,
    )
  }
  const cells = () => [...document.querySelectorAll('[data-metric]')].map((e) => e.textContent)

  it.each(['dev', 'test'])('a live %s run shows no metric value and asks for no indicator', async (split) => {
    const subs = vi.spyOn(storeFor(LOCKED_ID), 'setIndicators')
    renderBar(split, live, 'running', LOCKED_ID)
    await waitFor(() => expect(subs.mock.calls.length).toBeGreaterThan(0))
    await new Promise((r) => setTimeout(r, 100))
    expect(subs.mock.calls.every(([s]) => s.length === 0)).toBe(true)
    subs.mockRestore()
    expect(cells().length).toBe(10)
    expect(cells().every((c) => c === 'after the run ends')).toBe(true)
    expect(screen.getByText(new RegExp(`A ${split} run shows its metrics only after it ends`))).toBeInTheDocument()
    expect(lockedCalls()).toEqual([])
  })

  it('a live run whose split is not known yet (record not loaded) shows no value and asks for no indicator (CR-54 2)', async () => {
    const subs = vi.spyOn(storeFor(LOCKED_ID), 'setIndicators')
    renderBar(undefined, live, 'running', LOCKED_ID)
    await waitFor(() => expect(subs.mock.calls.length).toBeGreaterThan(0))
    await new Promise((r) => setTimeout(r, 100))
    expect(subs.mock.calls.every(([s]) => s.length === 0)).toBe(true)
    subs.mockRestore()
    expect(cells().every((c) => c === 'after the run ends')).toBe(true)
    expect(screen.getByText(/split of this run is not known yet/)).toBeInTheDocument()
    expect(lockedCalls()).toEqual([])
  })

  it.each([
    ['test', 'ended'],
    ['pilot', 'live'],
    ['exploratory', 'live'],
  ] as const)('a %s run that is %s shows its values', async (split, when) => {
    const subs = vi.spyOn(storeFor('run_MOCK01'), 'setIndicators')
    renderBar(split, when === 'ended' ? stream : live, when === 'ended' ? 'completed' : 'running')
    expect(screen.queryByText('after the run ends')).toBeNull()
    if (when === 'live') await waitFor(() => expect(subs.mock.calls.some(([s]) => s.length > 0)).toBe(true))
    subs.mockRestore()
    if (when === 'ended') await waitFor(() => expect(document.querySelector('[data-metric="delta_p"]')!.textContent).toMatch(/^-?\d+\.\d{3}$/))
  })
})

describe('public view hides a pending price (CR-49 3)', () => {
  const all = makeEpisode({ runId: 'run_PEND', kind: 'exploratory', seed: 1, rounds: 30, start: '2026-10-23T09:00:00Z', conditionId: null, branch: null })
  const start6 = all.findIndex((e) => e.type === 'round_started' && e.t === 6)
  const actA = all.findIndex((e, i) => i > start6 && e.type === 'acted' && e.agent_id === 'A')
  const live = foldEvents(all.slice(0, actA + 1))

  it('the card shows the price only after the round settles; god view shows it at once', () => {
    expect(agentRound(live, 'A', 6).price).not.toBeNull()
    expect(agentRound(live, 'A', 6, true).price).toBeNull()
    expect(agentRound(live, 'A', 5, true).price).toBe(live.prices.A!.find((p) => p.t === 5)!.value)
  })

  it('the public log has no act line for the round in progress', () => {
    const acted = (es: SimEvent[], t: number) => es.some((e) => e.type === 'acted' && e.t === t)
    expect(acted(live.events, 6)).toBe(true)
    expect(acted(publicEvents(live, live.events), 6)).toBe(false)
    expect(acted(publicEvents(live, live.events), 5)).toBe(true)
  })
})

describe('workbench page, live run (CR-49 1)', () => {
  // A live mock run held at a fixed event count. Raising the count lets the mock socket send the next events.
  const id = 'run_LIVEPAGE'
  const all = makeEpisode({ runId: id, kind: 'exploratory', seed: 3, rounds: 12, start: '2026-10-23T09:00:00Z', conditionId: null, branch: null })
  const after = (pred: (e: SimEvent) => boolean) => all.findIndex(pred) + 1
  const inRound6 = after((e) => e.type === 'acted' && e.agent_id === 'A' && e.t === 6) // A acted, B did not
  const inRound9 = after((e) => e.type === 'round_started' && e.t === 9)

  function hold(count: number) {
    const r = mockRuns.find((x) => x.id === id)!
    r.paused = { time: Date.now(), count }
  }

  async function renderPage(search: string) {
    seen.slider.length = 0
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const router = createMemoryRouter([{ path: '/workbench/:runId', element: <WorkbenchPage /> }], { initialEntries: [`/workbench/${id}${search}`] })
    render(
      <QueryClientProvider client={qc}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )
  }
  const footer = (agent: string) => screen.getByRole('region', { name: `Agent ${agent}` }).lastElementChild!.textContent

  beforeAll(() => {
    const ev = all.findIndex((e) => e.type === 'acted' && e.agent_id === 'B' && e.t === 6)
    expect(ev).toBeGreaterThan(inRound6 - 1)
    if (!mockRuns.some((x) => x.id === id)) {
      const r = { ...mockRuns.find((x) => x.id === 'run_MOCK02')!, id, header: foldEvents(all).header!, events: all, live: true, startedAt: Date.now() }
      mockRuns.push(r)
    }
  })

  it('following, the cards show the round in progress, then move on when the stream grows', async () => {
    hold(inRound6)
    await renderPage('')
    await waitFor(() => expect(footer('B')).toMatch(/^R6 price –thinking/))
    expect(footer('A')).toMatch(/^R6 price \d/)
    expect(screen.getByText(/round 6 in progress/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Stop' })).toBeInTheDocument()
    expect(seen.slider.at(-1)).toEqual({ t: null, maxT: 5 })
    hold(inRound9)
    await waitFor(() => expect(footer('B')).toMatch(/^R9 price –thinking/))
    expect(seen.slider.at(-1)).toEqual({ t: null, maxT: 8 })
  })

  it('a picked round stays while the stream grows', async () => {
    hold(inRound6)
    await renderPage('?t=3')
    await waitFor(() => expect(seen.slider.at(-1)).toEqual({ t: 3, maxT: 5 }))
    expect(footer('B')).toMatch(/^R3 price \d/)
    hold(inRound9)
    await waitFor(() => expect(seen.slider.at(-1)).toEqual({ t: 3, maxT: 8 }))
    expect(footer('B')).toMatch(/^R3 price \d/)
    expect(within(screen.getByRole('list', { name: 'Event log' })).getAllByRole('listitem')[0]).toHaveTextContent(/R3 · \d+ events? · at the scrubber/)
    expect(screen.queryByText(/in progress/)).toBeNull()
  })
})

describe('the last round of an ended run (CR-87 minor 5)', () => {
  const full = foldEvents(makeEpisode({ runId: 'run_FIN', kind: 'exploratory', seed: 1, rounds: 12, start: '2026-10-23T09:00:00Z', conditionId: null, branch: null }))

  it('is the last settled round with how the run ended, for completed, stopped and failed only', () => {
    expect(full.settledT).toBe(12)
    for (const status of ['completed', 'stopped', 'failed'] as const) expect(finalRound(full, status)).toEqual({ t: 12, status })
    for (const status of ['queued', 'running', 'paused', 'recovering'] as const) expect(finalRound(full, status), status).toBeNull()
    expect(finalRound(full, null)).toBeNull()
    expect(finalRound(full, undefined)).toBeNull()
  })

  it('the page gives it to the cards: an ended run says "posted in the last round", and a run in the middle does not', async () => {
    const id = 'run_0000000000000000DEM0PARENT'
    const render1 = async (at: string) => {
      const router = createMemoryRouter([{ path: '/workbench/:runId', element: <WorkbenchPage /> }], { initialEntries: [`/workbench/${id}${at}`] })
      render(
        <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
          <RouterProvider router={router} />
        </QueryClientProvider>,
      )
      await screen.findByRole('region', { name: 'Agent A' })
    }
    await render1('')
    await screen.findAllByText(/^R30, /)
    expect(screen.getAllByText(/^R30, posted in the last round: the run ended before any round could deliver it$/).length).toBeGreaterThan(0)
    expect(screen.queryByText(/^R30, delivered to nobody$/)).toBeNull()
    cleanup()
    await render1('?t=29')
    await screen.findAllByText(/^R\d+ (to|from) /)
    expect(screen.queryByText(/^R30[ ,]/)).toBeNull()
    expect(screen.queryByText(/posted in the last round/)).toBeNull()
  })
})
