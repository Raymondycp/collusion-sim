import { useMemo, useSyncExternalStore } from 'react'
import { RunSocket, type SocketPhase, type Subscription } from '@/api/socket'
import type { IndicatorUpdate } from '@/api/types'
import { applyEvents, clearGap, emptyRunState, type RunState } from './fold'

/** Indicator values built from WebSocket updates, in the shape of the REST instance (arrays aligned with t). */
export type LiveIndicator = { indicator_id: string; t: number[]; outputs: Record<string, (number | null)[]> }

export type RunStream = {
  state: RunState
  phase: SocketPhase
  detail: string | null
  /** By subscription key. Only for subscriptions set with setIndicators. */
  indicators: Readonly<Record<string, LiveIndicator>>
  indicatorError: string | null
}

/** A store with no subscriber for this long closes its socket and leaves the map. */
const EVICT_MS = 30_000

function addUpdate(prev: LiveIndicator | undefined, u: IndicatorUpdate): LiveIndicator {
  const base = prev ?? { indicator_id: u.indicator_id, t: [], outputs: {} }
  let i = base.t.length
  while (i > 0 && base.t[i - 1]! >= u.t) i--
  const replace = base.t[i] === u.t
  const t = [...base.t]
  t.splice(i, replace ? 1 : 0, u.t)
  const outputs: LiveIndicator['outputs'] = {}
  for (const name of new Set([...Object.keys(base.outputs), ...Object.keys(u.values)])) {
    const col = [...(base.outputs[name] ?? base.t.map(() => null))]
    col.splice(i, replace ? 1 : 0, u.values[name] ?? null)
    outputs[name] = col
  }
  return { indicator_id: base.indicator_id, t, outputs }
}

const fresh = (): RunStream => ({ state: emptyRunState, phase: 'connecting', detail: null, indicators: {}, indicatorError: null })

class RunStore {
  private snapshot: RunStream = fresh()
  private listeners = new Set<() => void>()
  private socket: RunSocket | null = null
  private refs = 0
  private evictTimer: ReturnType<typeof setTimeout> | null = null
  private subs: Subscription[] = []

  constructor(private readonly runId: string) {}

  subscribe = (fn: () => void) => {
    this.listeners.add(fn)
    if (this.evictTimer) clearTimeout(this.evictTimer)
    this.evictTimer = null
    // An evicted store that a page still holds joins the map again, unless a newer store took its place.
    if (!stores.has(this.runId)) stores.set(this.runId, this)
    if (this.refs++ === 0) this.start()
    return () => {
      this.listeners.delete(fn)
      if (--this.refs === 0) {
        this.stop()
        this.evictTimer = setTimeout(() => {
          if (this.refs === 0 && stores.get(this.runId) === this) stores.delete(this.runId)
        }, EVICT_MS)
      }
    }
  }

  getSnapshot = () => this.snapshot

  /** Live indicator values over the socket. Keys that leave the list drop their values. */
  setIndicators(subs: Subscription[]) {
    this.subs = subs
    const keep = new Set(subs.map((s) => s.key))
    const indicators = Object.fromEntries(Object.entries(this.snapshot.indicators).filter(([k]) => keep.has(k)))
    if (Object.keys(indicators).length !== Object.keys(this.snapshot.indicators).length) this.set({ indicators })
    this.socket?.setSubscriptions(subs)
  }

  private set(next: Partial<RunStream>) {
    this.snapshot = { ...this.snapshot, ...next }
    for (const fn of this.listeners) fn()
  }

  private start() {
    this.socket = new RunSocket(this.runId, this.snapshot.state.lastSeq + 1, {
      onEvents: (events) => {
        const state = applyEvents(this.snapshot.state, events)
        if (state.gapFrom !== null) {
          this.socket?.resumeFrom(state.gapFrom)
          this.set({ state: clearGap(state) })
        } else if (state !== this.snapshot.state) {
          this.set({ state })
        }
      },
      onPhase: (phase, detail) => this.set({ phase, detail: detail ?? null }),
      onReset: () => this.set({ state: emptyRunState, indicators: {} }),
      onIndicator: (u) => {
        if (!this.subs.some((s) => s.key === u.key)) return
        this.set({ indicators: { ...this.snapshot.indicators, [u.key]: addUpdate(this.snapshot.indicators[u.key], u) }, indicatorError: null })
      },
      onIndicatorError: (message) => this.set({ indicatorError: message }),
    })
    this.socket.setSubscriptions(this.subs)
  }

  private stop() {
    this.socket?.close()
    this.socket = null
  }
}

const stores = new Map<string, RunStore>()

export function storeFor(runId: string) {
  let s = stores.get(runId)
  if (!s) {
    s = new RunStore(runId)
    stores.set(runId, s)
  }
  return s
}

const idle: RunStream = { ...fresh(), phase: 'ended' }
const noop = () => () => {}

/** Several runs at once (branch overlays, compare). Returns one stream per id, in the same order. */
export function useRunStreams(ids: readonly string[]): RunStream[] {
  const key = ids.join(',')
  const sub = useMemo(() => {
    const list = key ? key.split(',').map(storeFor) : []
    let cache: RunStream[] = []
    return {
      subscribe: (fn: () => void) => {
        const offs = list.map((s) => s.subscribe(fn))
        return () => offs.forEach((off) => off())
      },
      getSnapshot: () => {
        const next = list.map((s) => s.getSnapshot())
        if (next.length !== cache.length || next.some((x, i) => x !== cache[i])) cache = next
        return cache
      },
    }
  }, [key])
  return useSyncExternalStore(sub.subscribe, sub.getSnapshot)
}

/** Live and replay use the same path: the socket replays stored events, then streams new ones. */
export function useRunStream(runId: string | null): RunStream {
  const store = useMemo(() => (runId ? storeFor(runId) : null), [runId])
  return useSyncExternalStore(store?.subscribe ?? noop, store?.getSnapshot ?? (() => idle))
}
