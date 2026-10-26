import { wsUrl } from './client'
import type { IndicatorUpdate, SimEvent, WsClientMessage, WsServerMessage } from './types'

export type SocketPhase = 'connecting' | 'replaying' | 'live' | 'ended' | 'reconnecting' | 'not_found' | 'error'

/** One indicator instance on the socket. `key` is chosen by the client and names the instance in every update. */
export type Subscription = { key: string; id: string; params: Record<string, unknown> }

type Handlers = {
  onEvents: (events: SimEvent[]) => void
  onPhase: (phase: SocketPhase, detail?: string) => void
  /** The stream restarts from seq 0: drop everything folded so far. */
  onReset?: () => void
  onIndicator?: (u: IndicatorUpdate) => void
  onIndicatorError?: (message: string) => void
}

const MAX_BACKOFF_MS = 10_000
/** Close code: from_seq is past the server's last stored seq + 1 (api/README.md §5). */
const FROM_SEQ_TOO_LARGE = 4409

/**
 * One read-only stream per run. Replays stored events from `fromSeq`, then streams live ones.
 * Events are delivered in batches, once per animation frame.
 */
export class RunSocket {
  private ws: WebSocket | null = null
  private buffer: SimEvent[] = []
  private flushScheduled = false
  private attempts = 0
  private retryTimer: ReturnType<typeof setTimeout> | null = null
  private closed = false
  private nextSeq: number
  private resetDone = false
  private subs = new Map<string, Subscription>()

  constructor(
    private readonly runId: string,
    fromSeq: number,
    private readonly h: Handlers,
  ) {
    this.nextSeq = fromSeq
    this.open()
  }

  /** Called by the store when it finds a gap or a duplicate-free resume point. */
  resumeFrom(seq: number) {
    this.nextSeq = seq
    this.buffer = []
    this.ws?.close(4000, 'resync')
  }

  /** Replaces the indicator subscriptions. The server answers each new key with one update per stored round. */
  setSubscriptions(next: Subscription[]) {
    const wanted = new Map(next.map((s) => [s.key, s]))
    const removed = [...this.subs.keys()].filter((k) => !wanted.has(k))
    const added = next.filter((s) => !this.subs.has(s.key))
    this.subs = wanted
    if (removed.length) this.send({ type: 'unsubscribe', keys: removed })
    if (added.length) this.send({ type: 'subscribe', subscriptions: added })
  }

  close() {
    this.closed = true
    if (this.retryTimer) clearTimeout(this.retryTimer)
    const ws = this.ws
    this.ws = null
    if (!ws) return
    // Closing a socket that is still connecting logs a browser warning, so wait for it to open.
    if (ws.readyState === WebSocket.CONNECTING) {
      ws.onmessage = null
      ws.onopen = () => ws.close(1000)
    } else ws.close(1000)
  }

  private send(m: WsClientMessage) {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(m))
  }

  private open() {
    this.h.onPhase(this.attempts === 0 ? 'connecting' : 'reconnecting')
    const ws = new WebSocket(wsUrl(`/ws/runs/${encodeURIComponent(this.runId)}?from_seq=${this.nextSeq}`))
    this.ws = ws
    ws.onopen = () => {
      this.attempts = 0
      this.h.onPhase('replaying')
      // A new connection has no subscriptions; the server replays every stored round for each key.
      if (this.subs.size) this.send({ type: 'subscribe', subscriptions: [...this.subs.values()] })
    }
    ws.onmessage = (m) => this.onMessage(m.data)
    ws.onclose = (ev) => {
      if (this.ws !== ws) return
      this.flush()
      this.ws = null
      if (this.closed) return
      if (ev.code === 1000) return this.h.onPhase('ended')
      if (ev.code === 4404) return this.h.onPhase('not_found')
      // 1007: the server could not parse a message from this page. A retry would send it again.
      if (ev.code === 1007) return this.h.onPhase('error', 'The backend refused a message from this page (WebSocket code 1007). Reload the page.')
      if (ev.code === FROM_SEQ_TOO_LARGE) return this.restartFromZero()
      this.scheduleRetry()
    }
  }

  /** The server has fewer events than this page folded (for example after a backend reset). Reload once from seq 0. */
  private restartFromZero() {
    if (this.resetDone) return this.h.onPhase('error', 'The backend has fewer events for this run than this page. Reload the page.')
    this.resetDone = true
    this.nextSeq = 0
    this.buffer = []
    this.h.onReset?.()
    this.open()
  }

  private scheduleRetry() {
    const delay = Math.min(MAX_BACKOFF_MS, 500 * 2 ** this.attempts++)
    this.h.onPhase('reconnecting')
    this.retryTimer = setTimeout(() => this.open(), delay)
  }

  private onMessage(raw: unknown) {
    if (typeof raw !== 'string') return
    let msg: WsServerMessage
    try {
      msg = JSON.parse(raw) as WsServerMessage
    } catch {
      return
    }
    switch (msg.type) {
      case 'event':
        this.nextSeq = Math.max(this.nextSeq, msg.event.seq + 1)
        this.buffer.push(msg.event)
        this.scheduleFlush()
        break
      case 'caught_up':
        this.flush()
        this.h.onPhase('live')
        break
      case 'ping':
        this.send({ type: 'pong' })
        break
      case 'indicator_update':
        // The update follows its round's settled event, so deliver the buffered events first.
        this.flush()
        this.h.onIndicator?.(msg)
        break
      case 'error':
        if (msg.code === 'unknown_indicator' || msg.code === 'indicator_params_invalid') this.h.onIndicatorError?.(msg.message)
        else this.h.onPhase('error', msg.message)
        break
    }
  }

  private scheduleFlush() {
    if (this.flushScheduled) return
    this.flushScheduled = true
    const run = () => {
      this.flushScheduled = false
      this.flush()
    }
    if (typeof requestAnimationFrame === 'function' && document.visibilityState === 'visible') requestAnimationFrame(run)
    else setTimeout(run, 50)
  }

  private flush() {
    if (this.buffer.length === 0) return
    const batch = this.buffer
    this.buffer = []
    this.h.onEvents(batch)
  }
}
