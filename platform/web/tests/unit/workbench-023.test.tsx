import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { makeEpisode, type MockRunSpec } from '@/mocks/episode'
import { foldEvents } from '@/run/fold'
import { agentRound, inbox, noTalkTurn, observationsAt } from '@/features/workbench/derive'
import type { SimEvent } from '@/api/types'
import { AgentCard } from '@/features/workbench/Panels'

afterEach(cleanup)

const episode = (conditionId: string, over: Partial<MockRunSpec> = {}) =>
  foldEvents(makeEpisode({ runId: 'run_23', kind: 'research', seed: 101, rounds: 30, start: '2026-10-23T09:00:00Z', conditionId, branch: null, ...over }))

const clearChild = (conditionId: string) =>
  episode(conditionId, { rounds: 8, branch: { parentRunId: 'run_P', forkT: 2, intervention: { type: 'ablation', channel_id: 'private_A_B', mode: 'clear' }, sampleIndex: 0 } })

/** A C3-silent episode with its raw events changed before the fold. */
const silentWith = (change: (events: SimEvent[]) => SimEvent[]) =>
  foldEvents(change(makeEpisode({ runId: 'run_23', kind: 'research', seed: 101, rounds: 30, start: '2026-10-23T09:00:00Z', conditionId: 'C3-silent', branch: null })))

const shuffleChild = (channel_id: string) =>
  episode('C1', { rounds: 6, branch: { parentRunId: 'run_P', forkT: 10, intervention: { type: 'ablation', channel_id, mode: 'shuffle' }, sampleIndex: 0 } })

/** Opens the "What A saw" fold of the card and returns it. */
function openFold(agentId: string) {
  const card = screen.getByRole('region', { name: `Agent ${agentId}` })
  const summary = within(card).getByText(`What ${agentId} saw`, { exact: false })
  fireEvent.click(summary)
  return summary.closest('details')!
}

describe('C3-silent: offer unseen (0.23.0, CR-57 n1, CR-58 major 2)', () => {
  const silent = episode('C3-silent')

  it('B has no talk turn: its only call in a round is the pricing call, and it reads no message', () => {
    expect(observationsAt(silent, 'B', 3).map((e) => e.purpose)).toEqual(['act'])
    expect(observationsAt(silent, 'B', 3)[0]!.observation).toMatchObject({ new_messages: [], recent_messages: [] })
    expect(observationsAt(silent, 'A', 3).map((e) => e.purpose)).toEqual(['talk', 'act'])
    expect(agentRound(silent, 'B', 3).calls).toBe(3)
    expect(agentRound(silent, 'A', 3).calls).toBe(6)
  })

  it("A's post says not delivered, because B has no talk turn; a post with an empty list elsewhere still says delivered to nobody", () => {
    const out = inbox(silent, 'A', 30).filter((m) => m.direction === 'out')
    expect(out.length).toBeGreaterThan(0)
    expect(out.every((m) => m.receivers?.length === 0 && m.unseenBy.map((u) => u.agentId).join() === 'B')).toBe(true)
    render(<AgentCard state={silent} agentId="A" cursorT={30} publicView={false} />)
    expect(screen.getByText('R2, not delivered: B has no talk turn (offer unseen)')).toBeInTheDocument()
    cleanup()
    const c1 = episode('C1', { rounds: 21 })
    expect(inbox(c1, 'A', 21).find((m) => m.t === 21)).toMatchObject({ receivers: [], unseenBy: [] })
  })

  it('a post of the last round of an ended run says so, not "delivered to nobody" (FE-realbackend finding 1)', () => {
    const c1 = episode('C1', { rounds: 21 })
    const head = (final: { t: number; status: string } | null) => {
      render(<AgentCard state={c1} agentId="A" cursorT={21} publicView={false} final={final} />)
      const heads = screen.getAllByText(/^R21, /).map((n) => n.textContent)
      cleanup()
      return heads
    }
    expect(head({ t: 21, status: 'completed' })).toContain('R21, posted in the last round: the run ended before any round could deliver it')
    expect(head(null)).toContain('R21, delivered to nobody')
    expect(head({ t: 22, status: 'completed' })).toContain('R21, delivered to nobody')
    expect(head({ t: 21, status: 'completed' })).not.toContain('R21, delivered to nobody')
  })

  it('a run that stopped or failed names how it ended, and does not call its round "the last round" (CR-87 minor 5)', () => {
    const c1 = episode('C1', { rounds: 21 })
    for (const status of ['stopped', 'failed']) {
      render(<AgentCard state={c1} agentId="A" cursorT={21} publicView={false} final={{ t: 21, status }} />)
      expect(screen.getByText(`R21, posted in the last settled round: the run ${status} before any round could deliver it`)).toBeInTheDocument()
      expect(screen.queryByText(/posted in the last round:/)).toBeNull()
      cleanup()
    }
  })

  it('a real cause beats the last-round text', () => {
    const lastPost = Math.max(...inbox(silent, 'A', 30).filter((m) => m.direction === 'out').map((m) => m.t))
    render(<AgentCard state={silent} agentId="A" cursorT={30} publicView={false} final={{ t: lastPost, status: 'completed' }} />)
    expect(screen.queryByText(/posted in the last round/)).toBeNull()
    expect(screen.getByText(`R${lastPost}, not delivered: B has no talk turn (offer unseen)`)).toBeInTheDocument()
  })

  it('without c3_isolation a seller with no post grant reads the channel in its pricing call, so the post is not "unseen"', () => {
    const open = { ...silent, header: { ...silent.header!, c3_isolation: false } }
    expect(inbox(open, 'A', 30).filter((m) => m.direction === 'out').every((m) => m.unseenBy.length === 0)).toBe(true)
  })

  it("B's card says it has no talk turn and sees no channel messages", () => {
    expect(inbox(silent, 'B', 30)).toEqual([])
    render(<AgentCard state={silent} agentId="B" cursorT={30} publicView={false} />)
    expect(screen.getByText('Seller B has no talk turn, so it sees no channel messages.')).toBeInTheDocument()
  })

  it("B's pricing call says why it has no messages", () => {
    render(<AgentCard state={silent} agentId="B" cursorT={8} publicView={false} />)
    const fold = openFold('B')
    expect(within(fold).getByText('Pricing call')).toBeInTheDocument()
    expect(within(fold).queryByText('Talk call')).toBeNull()
    expect(within(fold).getByText('No messages: the pricing call does not see the channel.')).toBeInTheDocument()
  })
})

describe('What A saw (0.23.0 Observation)', () => {
  const c1 = episode('C1')

  it('each call of the round shows the news so far, the recent messages (own as "you") and the own rounds', () => {
    render(<AgentCard state={c1} agentId="A" cursorT={8} publicView={false} />)
    const fold = openFold('A')
    expect(within(fold).getByText('What A saw at R8')).toBeInTheDocument()
    expect(within(fold).getAllByText(/^(Talk|Pricing) call$/).map((n) => n.textContent)).toEqual(['Talk call', 'Pricing call'])
    expect(within(fold).getAllByText('R6: Crude oil prices rose about 8% this week.')).toHaveLength(2)
    expect(within(fold).getAllByText('R6, you in private_A_B: Costs went up for both of us. Shall we both go to 10?')).toHaveLength(2)
    expect(within(fold).getAllByText('R7, B in private_A_B: Yes, 10 from now on.')).toHaveLength(2)
    // Values have at most 2 decimals; the record can hold float noise.
    expect(within(fold).getAllByText(/^R7: price \d+(\.\d\d?)?, sold \d+(\.\d\d?)?, profit -?\d+(\.\d\d?)?$/)).toHaveLength(2)
  })

  it('rival prices: each rival, every round so far (0.31.0), with at most 2 decimals', () => {
    render(<AgentCard state={c1} agentId="A" cursorT={8} publicView={false} />)
    const fold = openFold('A')
    expect(within(fold).getAllByText('Rival prices')).toHaveLength(2)
    const lines = within(fold).getAllByText(/^B: R1 /)
    expect(lines).toHaveLength(2)
    expect(lines[0]!.textContent).toMatch(/^B: R1 \d+(\.\d\d?)?(, R\d+ \d+(\.\d\d?)?){6}$/)
    expect(lines[0]!.textContent).not.toContain('R8')
  })

  it('a pull run says "not pushed", not "episode before 0.23.0" (CR-83 2)', () => {
    const pull = foldEvents(
      makeEpisode({ runId: 'run_23', kind: 'research', seed: 101, rounds: 10, start: '2026-10-23T09:00:00Z', conditionId: 'C1', branch: null }).map((e) => {
        if (e.type === 'run_started') return { ...e, header: { ...e.header, observation_mode: 'pull' as const } }
        if (e.type === 'observed') return { ...e, observation: { ...e.observation, news: null, recent_messages: null, own_history: null, rival_history: null, mask_noop: null } }
        return e
      }),
    )
    render(<AgentCard state={pull} agentId="A" cursorT={8} publicView={false} />)
    const fold = openFold('A')
    const text = 'Not pushed: observation mode pull. The agent reads with its tools. See the Trail button.'
    expect(within(fold).getAllByText(text)).toHaveLength(8)
    expect(within(fold).queryByText(/episode before/)).toBeNull()
  })

  it('a push run with null fields still says the episode is old', () => {
    const old = foldEvents(
      makeEpisode({ runId: 'run_23', kind: 'research', seed: 101, rounds: 10, start: '2026-10-23T09:00:00Z', conditionId: 'C1', branch: null }).map((e) => {
        if (e.type === 'run_started') return { ...e, header: { ...e.header, observation_mode: 'push' as const } }
        return e.type === 'observed' ? { ...e, observation: { ...e.observation, rival_history: null } } : e
      }),
    )
    render(<AgentCard state={old} agentId="A" cursorT={8} publicView={false} />)
    expect(within(openFold('A')).getAllByText('Not recorded (episode before 0.31.0)')).toHaveLength(2)
  })

  it('rival prices: float noise in the record shows with 2 decimals', () => {
    const noisy = foldEvents(
      makeEpisode({ runId: 'run_23', kind: 'research', seed: 101, rounds: 10, start: '2026-10-23T09:00:00Z', conditionId: 'C1', branch: null }).map((e) =>
        e.type === 'observed' && e.agent_id === 'A' && e.observation.rival_history ? { ...e, observation: { ...e.observation, rival_history: { B: [{ t: 1, price: 10.123456789 }] } } } : e,
      ),
    )
    render(<AgentCard state={noisy} agentId="A" cursorT={8} publicView={false} />)
    expect(within(openFold('A')).getAllByText('B: R1 10.12')).toHaveLength(2)
  })

  it('rival prices: the whole record, not the last 5 rounds', () => {
    render(<AgentCard state={c1} agentId="A" cursorT={16} publicView={false} />)
    const fold = openFold('A')
    expect(within(fold).getAllByText(/^B: R1 .*, R15 \d/)).toHaveLength(2)
    expect(within(fold).getAllByText(/^R1: price /)).toHaveLength(2)
  })

  it('rival prices: an empty object says that there is no grant; round 1 and an older episode do not', () => {
    const noGrant = foldEvents(
      makeEpisode({ runId: 'run_23', kind: 'research', seed: 101, rounds: 10, start: '2026-10-23T09:00:00Z', conditionId: 'C1', branch: null }).map((e) =>
        e.type === 'observed' && e.agent_id === 'A' ? { ...e, observation: { ...e.observation, rival_history: {} } } : e,
      ),
    )
    render(<AgentCard state={noGrant} agentId="A" cursorT={8} publicView={false} />)
    const fold = openFold('A')
    expect(within(fold).getAllByText('None: the seller has no get_rival_prices grant.')).toHaveLength(2)
    cleanup()
    render(<AgentCard state={c1} agentId="A" cursorT={1} publicView={false} />)
    expect(within(openFold('A')).queryByText(/get_rival_prices grant/)).toBeNull()
    cleanup()
    const older = foldEvents(makeEpisode({ runId: 'run_23', kind: 'research', seed: 101, rounds: 10, start: '2026-10-23T09:00:00Z', conditionId: 'C1', branch: null }).map((e) => (e.type === 'observed' && e.observation.rival_history ? { ...e, observation: { ...e.observation, rival_history: null } } : e)))
    render(<AgentCard state={older} agentId="A" cursorT={8} publicView={false} />)
    expect(within(openFold('A')).getAllByText('Not recorded (episode before 0.31.0)')).toHaveLength(2)
  })

  it('a price that the system set has the mark', () => {
    render(<AgentCard state={c1} agentId="A" cursorT={16} publicView={false} />)
    const fold = openFold('A')
    expect(within(fold).getAllByText(/^R15: price .* \(set by the system\)$/)).toHaveLength(2)
    expect(within(fold).getAllByText(/^R14: price [^(]*$/)).toHaveLength(2)
  })

  it('a C3 pricing call: no messages, and it says why; the talk call reads the channel', () => {
    render(<AgentCard state={episode('C3')} agentId="B" cursorT={8} publicView={false} />)
    const fold = openFold('B')
    expect(within(fold).getAllByText('No messages: the pricing call does not see the channel.')).toHaveLength(1)
    expect(within(fold).getAllByText(/^R6, A in private_A_B: /)).toHaveLength(1)
  })

  it('an episode stored before 0.23.0 says not recorded, not none', () => {
    render(<AgentCard state={episode('C1', { before023: true })} agentId="A" cursorT={8} publicView={false} />)
    const fold = openFold('A')
    expect(within(fold).getAllByText('Not recorded (episode before 0.23.0)')).toHaveLength(6)
    expect(within(fold).queryByText('None')).toBeNull()
  })

  it('round 1: no news and no own round yet gives "None"', () => {
    render(<AgentCard state={c1} agentId="A" cursorT={1} publicView={false} />)
    const fold = openFold('A')
    expect(within(fold).getAllByText('None')).toHaveLength(8)
  })

  it('public view has no fold: the messages hold secret-channel text', () => {
    render(<AgentCard state={c1} agentId="A" cursorT={8} publicView />)
    expect(screen.queryByText(/What A saw/)).toBeNull()
    expect(screen.queryByText(/Shall we both go to 10/)).toBeNull()
  })
})

describe('shuffle mask with no effect (Observation.mask_noop, CR-60 n1)', () => {
  it('fewer than 2 different texts from the other sellers: the footer and the fold say the shuffle changed nothing', () => {
    const pub = shuffleChild('public')
    expect(observationsAt(pub, 'A', 13).every((e) => e.observation.mask_noop)).toBe(true)
    render(<AgentCard state={pub} agentId="A" cursorT={13} publicView={false} />)
    expect(screen.getByText('shuffle had no effect')).toBeInTheDocument()
    const fold = openFold('A')
    expect(within(fold).getAllByText(/The shuffle mask changed nothing in this call/)).toHaveLength(2)
  })

  it('2 or more different texts: no note; the public view shows no note either', () => {
    const priv = shuffleChild('private_A_B')
    expect(observationsAt(priv, 'A', 10).some((e) => e.observation.mask_noop)).toBe(false)
    const { rerender } = render(<AgentCard state={priv} agentId="A" cursorT={10} publicView={false} />)
    expect(screen.queryByText('shuffle had no effect')).toBeNull()
    rerender(<AgentCard state={shuffleChild('public')} agentId="A" cursorT={13} publicView />)
    expect(screen.queryByText('shuffle had no effect')).toBeNull()
  })

  it("the reader's own messages keep their text; the other sender's text is shuffled", () => {
    const recent = observationsAt(shuffleChild('private_A_B'), 'A', 10)[0]!.observation.recent_messages!
    expect(recent.find((m) => m.sender === 'A' && m.t === 6)!.text).toBe('Costs went up for both of us. Shall we both go to 10?')
    expect(recent.find((m) => m.sender === 'B' && m.t === 7)!.text).not.toBe('Yes, 10 from now on.')
  })
})

describe('"unseen" from the record, the grants only as a fallback (CR-67 1)', () => {
  const outHeads = (state: ReturnType<typeof episode>) => {
    render(<AgentCard state={state} agentId="A" cursorT={state.settledT ?? 30} publicView={false} />)
    return screen.getAllByText(/^R\d+(, | to )/).map((n) => n.textContent)
  }

  it('the observed events win over the grants: B has a post grant but no talk call, so the offer is unseen', () => {
    const s = silentWith((events) =>
      events.map((e) => (e.type === 'run_started' ? { ...e, header: { ...e.header, agents: e.header.agents.map((a) => (a.id === 'B' ? { ...a, capability: { ...a.capability, grants: [...a.capability.grants, { tool_id: 'post', params: { channels: ['private_A_B'] } }] } } : a)) } } : e)),
    )
    expect(noTalkTurn(s, 'B')).toBe(true)
    expect(inbox(s, 'A', 30).filter((m) => m.direction === 'out').every((m) => m.unseenBy.map((u) => `${u.agentId}:${u.cause}`).join() === 'B:no_talk_turn')).toBe(true)
  })

  it('a talk call of B in the record means B has a talk turn, whatever the grants say', () => {
    const s = silentWith((events) => events.map((e) => (e.type === 'observed' && e.agent_id === 'B' ? { ...e, purpose: 'talk' as const } : e)))
    expect(noTalkTurn(s, 'B')).toBe(false)
    expect(inbox(s, 'A', 30).filter((m) => m.direction === 'out').every((m) => m.unseenBy.length === 0)).toBe(true)
  })

  it('no observed event yet (a live run): the grants decide', () => {
    const s = silentWith((events) => events.filter((e) => e.type !== 'observed'))
    expect(noTalkTurn(s, 'B')).toBe(true)
    expect(inbox(s, 'A', 30).filter((m) => m.direction === 'out').every((m) => m.unseenBy[0]?.cause === 'no_talk_turn')).toBe(true)
  })

  it('a member with no read grant for the channel and no message of it in any call: "no read grant"', () => {
    const c1 = makeEpisode({ runId: 'run_23', kind: 'research', seed: 101, rounds: 30, start: '2026-10-23T09:00:00Z', conditionId: 'C1', branch: null }).map((e): SimEvent => {
      if (e.type === 'run_started')
        return { ...e, header: { ...e.header, agents: e.header.agents.map((a) => (a.id === 'B' ? { ...a, capability: { ...a.capability, grants: a.capability.grants.filter((g) => g.tool_id !== 'read_channel') } } : a)) } }
      if (e.type === 'posted' && e.agent_id === 'A') return { ...e, receivers: [] }
      if (e.type === 'observed' && e.agent_id === 'B') return { ...e, observation: { ...e.observation, new_messages: [], recent_messages: [] } }
      return e
    })
    const heads = outHeads(foldEvents(c1))
    expect(heads).toContain('R2, not delivered: B has no read grant for this channel')
  })

  it('the record wins over a missing read grant: a member whose calls hold messages of the channel reads it', () => {
    const c1 = makeEpisode({ runId: 'run_23', kind: 'research', seed: 101, rounds: 30, start: '2026-10-23T09:00:00Z', conditionId: 'C1', branch: null }).map((e): SimEvent => {
      if (e.type === 'run_started')
        return { ...e, header: { ...e.header, agents: e.header.agents.map((a) => (a.id === 'B' ? { ...a, capability: { ...a.capability, grants: a.capability.grants.filter((g) => g.tool_id !== 'read_channel') } } : a)) } }
      if (e.type === 'posted' && e.agent_id === 'A') return { ...e, receivers: [] }
      return e
    })
    expect(inbox(foldEvents(c1), 'A', 30).filter((m) => m.direction === 'out').every((m) => m.unseenBy.length === 0)).toBe(true)
  })

  it('a clear mask and no talk turn: the line names both causes', () => {
    const heads = outHeads(clearChild('C3-silent'))
    expect(heads.length).toBeGreaterThan(0)
    expect(heads.every((h) => /^R\d+, not delivered: the clear mask removes this channel, and B has no talk turn \(offer unseen\) · talk call only$/.test(h!))).toBe(true)
  })

  it('the clear mask covers a post by its delivery round, as View.receivers does (CR-67 n2)', () => {
    const events = makeEpisode({ runId: 'run_23', kind: 'research', seed: 101, rounds: 8, start: '2026-10-23T09:00:00Z', conditionId: 'C1', branch: { parentRunId: 'run_P', forkT: 2, intervention: { type: 'ablation', channel_id: 'private_A_B', mode: 'clear' }, sampleIndex: 0 } })
    const first = events.find((e): e is Extract<SimEvent, { type: 'posted' }> => e.type === 'posted' && e.agent_id === 'A' && e.channel_id === 'private_A_B')!
    expect(first.t).toBe(2)
    // A post of the round before the fork, delivered in the fork round.
    const s = foldEvents(events.map((e) => (e === first ? { ...first, t: 1, deliver_t: 2 } : e)))
    expect(inbox(s, 'A', 8).find((m) => m.seq === first.seq)).toMatchObject({ t: 1, cleared: true })
  })

  it('a clear mask alone: the line names the mask, not "delivered to nobody"', () => {
    const heads = outHeads(clearChild('C1'))
    expect(heads.length).toBeGreaterThan(0)
    expect(heads.every((h) => /^R\d+, not delivered: the clear mask removes this channel$/.test(h!))).toBe(true)
  })
})

describe('one observed index; fold title; mock as a 0.23.0 research run (CR-67 2, 3)', () => {
  it('the (agent, round) index is built once per event list', () => {
    const s = episode('C1')
    expect(observationsAt(s, 'A', 8)).toBe(observationsAt(s, 'A', 8))
    expect(observationsAt(s, 'A', 8).map((e) => e.purpose)).toEqual(['talk', 'act'])
  })

  it('the fold says that its message list covers all channels', () => {
    render(<AgentCard state={episode('C1')} agentId="A" cursorT={8} publicView={false} />)
    const fold = openFold('A')
    expect(within(fold).getAllByText('Messages (all channels)')).toHaveLength(2)
  })

  it('a research mock has query limit 0 and no read call; an exploratory mock keeps them', () => {
    const research = episode('C1')
    expect(research.header!.limits!.max_queries_per_round).toBe(0)
    expect(research.events.some((e) => e.type === 'tool_called')).toBe(false)
    const free = foldEvents(makeEpisode({ runId: 'run_X', kind: 'exploratory', seed: 7, rounds: 5, start: '2026-10-23T09:00:00Z', conditionId: null, branch: null }))
    expect(free.events.some((e) => e.type === 'tool_called')).toBe(true)
  })
})
