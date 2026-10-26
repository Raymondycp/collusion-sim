import { describe, expect, it } from 'vitest'
import { applyEvents, emptyRunState, foldEvents, viewAt } from '@/run/fold'
import { makeEpisode } from '@/mocks/episode'

const main = makeEpisode({ runId: 'run_T1', kind: 'research', seed: 101, rounds: 30, start: '2026-10-23T09:00:00Z', conditionId: 'C1', branch: null })
const probe = makeEpisode({
  runId: 'run_T1P0',
  kind: 'research',
  seed: 101,
  rounds: 5,
  start: '2026-10-23T10:00:00Z',
  conditionId: 'C1',
  branch: { parentRunId: 'run_T1', forkT: 20, intervention: { type: 'probe', target: 'B', price: 'nash' }, sampleIndex: 0 },
})

describe('foldEvents', () => {
  it('builds one price point per seller per settled round, with reference values from the events', () => {
    const s = foldEvents(main)
    expect(s.sellers).toEqual(['A', 'B'])
    expect(s.prices.A).toHaveLength(30)
    expect(s.prices.B?.map((p) => p.t)).toEqual(Array.from({ length: 30 }, (_, i) => i + 1))
    expect(s.reference[0]).toEqual({ t: 1, nash: 5.75, joint_optimum: 10.5 })
    expect(s.reference.find((r) => r.t === 6)).toEqual({ t: 6, nash: 6.13, joint_optimum: 10.75 })
    // Consumer surplus comes from the tier-B `reference` event, not from `settled` (contract v0.6.0).
    const surplus = main.flatMap((e) => (e.type === 'reference' ? [{ t: e.t, value: e.consumer_surplus }] : []))
    expect(surplus).toHaveLength(30)
    expect(s.surplus).toEqual(surplus)
    expect(s.status).toBe('completed')
    expect(s.endReason).toBe('horizon')
  })

  it('keeps a round whose demand model has no closed forms, with null reference prices', () => {
    const noClosedForm = main.map((e) => {
      if (e.type !== 'round_started' && e.type !== 'reference') return e
      const { nash: _n, joint_optimum: _j, ...reference } = e.reference
      return { ...e, reference }
    })
    const s = foldEvents(noClosedForm)
    expect(s.reference).toHaveLength(30)
    expect(s.reference[0]).toEqual({ t: 1, nash: null, joint_optimum: null })
  })

  it('makes markers for news, posts and fallbacks, and keeps every message with its delivery time', () => {
    const s = foldEvents(main)
    expect(s.markers.filter((m) => m.kind === 'news').map((m) => m.t)).toEqual([6])
    expect(s.markers.filter((m) => m.kind === 'fallback')).toHaveLength(1)
    expect(s.fallbacks).toBe(1)
    const first = s.messages[0]!
    expect(first).toMatchObject({ t: 2, deliverT: 3, agentId: 'A', channelId: 'private_A_B' })
    expect(s.messages.length).toBe(s.markers.filter((m) => m.kind === 'post').length)
  })

  it('counts calls, cache hits and cost from llm_call events', () => {
    const s = foldEvents(main)
    const calls = main.filter((e) => e.type === 'llm_call')
    expect(s.calls).toBe(calls.length)
    expect(s.cacheHits).toBe(calls.filter((e) => e.type === 'llm_call' && e.cache_hit).length)
  })

  it('gives the same state whether events arrive at once or in small batches', () => {
    let s = emptyRunState
    for (let i = 0; i < main.length; i += 7) s = applyEvents(s, main.slice(i, i + 7))
    expect(s).toEqual(foldEvents(main))
  })

  it('skips duplicates from a reconnect replay', () => {
    const half = Math.floor(main.length / 2)
    let s = applyEvents(emptyRunState, main.slice(0, half))
    s = applyEvents(s, main.slice(half - 10))
    expect(s).toEqual(foldEvents(main))
  })

  it('stops at a gap and reports the first missing seq', () => {
    const s = applyEvents(emptyRunState, [...main.slice(0, 10), ...main.slice(12, 20)])
    expect(s.lastSeq).toBe(9)
    expect(s.gapFrom).toBe(10)
    expect(applyEvents(s, main.slice(10))).toBe(s)
  })

  it('reads a probe branch: the header carries the fork, the marker names the target', () => {
    const s = foldEvents(probe)
    expect(s.header?.branch?.fork_t).toBe(20)
    expect(s.prices.B?.[0]).toEqual({ t: 20, value: 5.75 })
    const m = s.markers.find((x) => x.kind === 'probe')
    expect(m).toMatchObject({ t: 20, agentId: 'B' })
  })

  it('viewAt(t) equals folding only the events up to round t (no look-ahead)', () => {
    const full = foldEvents(main)
    for (const t of [1, 6, 15, 29]) {
      const cut = viewAt(full, t)
      const ref = foldEvents(main.filter((e) => e.t <= t))
      expect(cut.prices).toEqual(ref.prices)
      expect(cut.reference).toEqual(ref.reference)
      expect(cut.markers).toEqual(ref.markers)
      expect(cut.messages).toEqual(ref.messages)
      expect(cut.events).toEqual(ref.events)
    }
  })

  it('keeps the reason of a run that waits as recovering (no run_ended)', () => {
    const cut = main.filter((e) => e.type !== 'run_ended')
    const last = cut[cut.length - 1]!
    const s = foldEvents([...cut, { seq: last.seq + 1, run_id: last.run_id, t: last.t, wall_time: last.wall_time, tier: 'B', type: 'run_status_changed', status: 'recovering', reason: 'paid_calls_disabled' }])
    expect(s.status).toBe('recovering')
    expect(s.statusReason).toBe('paid_calls_disabled')
  })

  it('does not mutate the previous state', () => {
    const a = applyEvents(emptyRunState, main.slice(0, 50))
    const snapshot = JSON.stringify(a)
    applyEvents(a, main.slice(50))
    expect(JSON.stringify(a)).toBe(snapshot)
  })
})
