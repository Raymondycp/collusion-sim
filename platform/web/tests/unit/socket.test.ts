import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { RunSocket, type SocketPhase } from '@/api/socket'
import type { IndicatorUpdate, SimEvent } from '@/api/types'
import { storeFor } from '@/run/useRunStream'
import { makeEpisode } from '@/mocks/episode'

/** Like a browser socket: readyState, and close events that arrive later, not inside close(). */
class FakeWebSocket {
  static CONNECTING = 0
  static OPEN = 1
  static CLOSING = 2
  static CLOSED = 3
  static all: FakeWebSocket[] = []
  readyState = FakeWebSocket.CONNECTING
  onopen: (() => void) | null = null
  onmessage: ((m: { data: string }) => void) | null = null
  onclose: ((e: { code: number }) => void) | null = null
  sent: string[] = []
  closedWith: number | null = null
  constructor(readonly url: string) {
    FakeWebSocket.all.push(this)
  }
  send(d: string) {
    if (this.readyState !== FakeWebSocket.OPEN) throw new Error('send on a socket that is not open')
    this.sent.push(d)
  }
  close(code = 1000) {
    this.closedWith = code
    this.readyState = FakeWebSocket.CLOSING
    queueMicrotask(() => this.serverClose(code))
  }
  serverOpen() {
    this.readyState = FakeWebSocket.OPEN
    this.onopen?.()
  }
  serverSend(m: unknown) {
    this.onmessage?.({ data: JSON.stringify(m) })
  }
  serverClose(code: number) {
    if (this.readyState === FakeWebSocket.CLOSED) return
    this.readyState = FakeWebSocket.CLOSED
    this.onclose?.({ code })
  }
  messages() {
    return this.sent.map((s) => JSON.parse(s) as { type: string })
  }
}

const events = makeEpisode({ runId: 'run_S1', kind: 'exploratory', seed: 1, rounds: 3, start: '2026-10-23T09:00:00Z', conditionId: null, branch: null })
const update = (t: number, v: number): IndicatorUpdate => ({ type: 'indicator_update', key: 'k1', indicator_id: 'delta_p', t, values: { delta_p: v } })

describe('RunSocket', () => {
  beforeEach(() => {
    FakeWebSocket.all = []
    vi.stubGlobal('WebSocket', FakeWebSocket)
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('reconnects after a dropped connection and asks only for events it has not seen', async () => {
    const got: SimEvent[] = []
    const phases: SocketPhase[] = []
    new RunSocket('run_S1', 0, { onEvents: (e) => got.push(...e), onPhase: (p) => phases.push(p) })
    const first = FakeWebSocket.all[0]!
    expect(first.url).toContain('/api/ws/runs/run_S1?from_seq=0')
    first.serverOpen()
    for (const e of events.slice(0, 10)) first.serverSend({ type: 'event', event: e })
    first.serverClose(1006)
    expect(phases).toContain('reconnecting')

    await vi.advanceTimersByTimeAsync(600)
    const second = FakeWebSocket.all[1]!
    expect(second.url).toContain('from_seq=10')
    second.serverOpen()
    for (const e of events.slice(10)) second.serverSend({ type: 'event', event: e })
    second.serverSend({ type: 'caught_up', last_seq: events.length - 1 })
    expect(phases.at(-1)).toBe('live')
    second.serverClose(1000)

    expect(got.map((e) => e.seq)).toEqual(events.map((e) => e.seq))
    expect(phases.at(-1)).toBe('ended')
    await vi.advanceTimersByTimeAsync(20_000)
    expect(FakeWebSocket.all).toHaveLength(2)
  })

  it('does not reconnect to a run that does not exist', async () => {
    const phases: SocketPhase[] = []
    new RunSocket('run_NONE', 0, { onEvents: () => {}, onPhase: (p) => phases.push(p) })
    FakeWebSocket.all[0]!.serverClose(4404)
    await vi.advanceTimersByTimeAsync(20_000)
    expect(phases.at(-1)).toBe('not_found')
    expect(FakeWebSocket.all).toHaveLength(1)
  })

  it('stops with an error, and does not retry, when the server cannot parse a message from the page', async () => {
    const phases: SocketPhase[] = []
    new RunSocket('run_S1', 0, { onEvents: () => {}, onPhase: (p) => phases.push(p) })
    FakeWebSocket.all[0]!.serverOpen()
    FakeWebSocket.all[0]!.serverClose(1007)
    await vi.advanceTimersByTimeAsync(20_000)
    expect(phases.at(-1)).toBe('error')
    expect(FakeWebSocket.all).toHaveLength(1)
  })

  it('answers a ping with a pong', () => {
    new RunSocket('run_S1', 0, { onEvents: () => {}, onPhase: () => {} })
    const ws = FakeWebSocket.all[0]!
    ws.serverOpen()
    ws.serverSend({ type: 'ping', at: '2026-10-23T09:00:00Z' })
    expect(ws.messages()).toEqual([{ type: 'pong' }])
  })

  it('on 4409 resets and reloads from seq 0 once; a second 4409 is an error, with no more retries', async () => {
    const phases: [SocketPhase, string | undefined][] = []
    let resets = 0
    new RunSocket('run_S1', 40, { onEvents: () => {}, onPhase: (p, d) => phases.push([p, d]), onReset: () => resets++ })
    FakeWebSocket.all[0]!.serverClose(4409)
    expect(resets).toBe(1)
    expect(FakeWebSocket.all[1]!.url).toContain('from_seq=0')
    FakeWebSocket.all[1]!.serverClose(4409)
    await vi.advanceTimersByTimeAsync(20_000)
    expect(FakeWebSocket.all).toHaveLength(2)
    expect(resets).toBe(1)
    expect(phases.at(-1)).toEqual(['error', 'The backend has fewer events for this run than this page. Reload the page.'])
  })

  it('resumeFrom reopens at the given seq without the old buffer', async () => {
    const got: SimEvent[] = []
    const s = new RunSocket('run_S1', 0, { onEvents: (e) => got.push(...e), onPhase: () => {} })
    const first = FakeWebSocket.all[0]!
    first.serverOpen()
    first.serverSend({ type: 'event', event: events[0] })
    first.serverSend({ type: 'event', event: events[5] })
    s.resumeFrom(1)
    expect(first.closedWith).toBe(4000)
    await vi.advanceTimersByTimeAsync(600)
    expect(FakeWebSocket.all[1]!.url).toContain('from_seq=1')
    expect(got.map((e) => e.seq)).toEqual([])
  })

  it('close() on a connecting socket waits for it to open, then closes it, and never reconnects', async () => {
    const s = new RunSocket('run_S1', 0, { onEvents: () => {}, onPhase: () => {} })
    const ws = FakeWebSocket.all[0]!
    s.close()
    expect(ws.closedWith).toBeNull()
    ws.serverOpen()
    expect(ws.closedWith).toBe(1000)
    await vi.advanceTimersByTimeAsync(20_000)
    expect(FakeWebSocket.all).toHaveLength(1)
  })

  it('sends subscriptions when open, the changes later, and all of them again after a reconnect', async () => {
    const got: IndicatorUpdate[] = []
    const s = new RunSocket('run_S1', 0, { onEvents: () => {}, onPhase: () => {}, onIndicator: (u) => got.push(u) })
    s.setSubscriptions([{ key: 'k1', id: 'delta_p', params: { smooth: 3 } }])
    const first = FakeWebSocket.all[0]!
    first.serverOpen()
    expect(first.messages()).toEqual([{ type: 'subscribe', subscriptions: [{ key: 'k1', id: 'delta_p', params: { smooth: 3 } }] }])

    s.setSubscriptions([{ key: 'k2', id: 'msg_volume', params: {} }])
    expect(first.messages().slice(1)).toEqual([
      { type: 'unsubscribe', keys: ['k1'] },
      { type: 'subscribe', subscriptions: [{ key: 'k2', id: 'msg_volume', params: {} }] },
    ])
    first.serverSend(update(1, 0.5))
    expect(got).toEqual([update(1, 0.5)])

    first.serverClose(1006)
    await vi.advanceTimersByTimeAsync(600)
    FakeWebSocket.all[1]!.serverOpen()
    expect(FakeWebSocket.all[1]!.messages()).toEqual([{ type: 'subscribe', subscriptions: [{ key: 'k2', id: 'msg_volume', params: {} }] }])
  })

  it('an indicator error goes to the indicator, not to the stream', () => {
    const phases: SocketPhase[] = []
    const errors: string[] = []
    new RunSocket('run_S1', 0, { onEvents: () => {}, onPhase: (p) => phases.push(p), onIndicatorError: (m) => errors.push(m) })
    const ws = FakeWebSocket.all[0]!
    ws.serverOpen()
    ws.serverSend({ type: 'error', code: 'unknown_indicator', message: 'unknown indicator x' })
    expect(errors).toEqual(['unknown indicator x'])
    expect(phases).not.toContain('error')
  })
})

describe('run store', () => {
  beforeEach(() => {
    FakeWebSocket.all = []
    vi.stubGlobal('WebSocket', FakeWebSocket)
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('builds live indicator values by round, replaces a repeated round, and forgets a dropped key', () => {
    const store = storeFor('run_ST1')
    const off = store.subscribe(() => {})
    store.setIndicators([{ key: 'k1', id: 'delta_p', params: {} }])
    const ws = FakeWebSocket.all[0]!
    ws.serverOpen()
    ws.serverSend(update(2, 0.4))
    ws.serverSend(update(1, 0.2))
    ws.serverSend(update(2, 0.5))
    ws.serverSend({ ...update(3, 0.6), key: 'other' })
    expect(store.getSnapshot().indicators).toEqual({ k1: { indicator_id: 'delta_p', t: [1, 2], outputs: { delta_p: [0.2, 0.5] } } })
    store.setIndicators([])
    expect(store.getSnapshot().indicators).toEqual({})
    off()
  })

  it('on 4409 drops the folded state and the indicators, then refolds from seq 0', async () => {
    const store = storeFor('run_ST2')
    const off = store.subscribe(() => {})
    store.setIndicators([{ key: 'k1', id: 'delta_p', params: {} }])
    const first = FakeWebSocket.all[0]!
    first.serverOpen()
    for (const e of events.slice(0, 8)) first.serverSend({ type: 'event', event: e })
    first.serverSend({ type: 'caught_up', last_seq: 7 })
    first.serverSend(update(1, 0.2))
    expect(store.getSnapshot().state.lastSeq).toBe(7)
    first.serverClose(4409)
    expect(store.getSnapshot().state.lastSeq).toBe(-1)
    expect(store.getSnapshot().indicators).toEqual({})
    const second = FakeWebSocket.all[1]!
    second.serverOpen()
    for (const e of events.slice(0, 3)) second.serverSend({ type: 'event', event: e })
    await vi.advanceTimersByTimeAsync(100)
    expect(store.getSnapshot().state.lastSeq).toBe(2)
    off()
  })

  it('closes its socket with no subscriber and leaves the map after a while', async () => {
    const store = storeFor('run_ST3')
    const off = store.subscribe(() => {})
    const ws = FakeWebSocket.all[0]!
    ws.serverOpen()
    off()
    expect(ws.closedWith).toBe(1000)
    expect(storeFor('run_ST3')).toBe(store)
    await vi.advanceTimersByTimeAsync(31_000)
    expect(storeFor('run_ST3')).not.toBe(store)
  })
})
