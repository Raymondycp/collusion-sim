import { http, HttpResponse } from 'msw/http'
import { ws } from 'msw/ws'
import type { Problem, SimEvent, WsServerMessage } from '@/api/types'

// Read-only snapshot mode (vite --mode snapshot): every /api answer comes from the files that
// scripts/record-snapshot.mjs wrote; nothing reaches a server. Writes answer 405.

export type Recorded = { status: number; body: unknown }
export type SnapshotIndex = { recorded_at: string; source: string; api_version: string; experiments: string[]; runs: string[]; responses: Record<string, Recorded> }
export type RunBundle = { responses: Record<string, Recorded>; events: SimEvent[] }
/** Reads one snapshot file (`index.json`, `runs/<run_id>.json.gz`) as JSON. */
export type LoadFile = <T>(name: string) => Promise<T>

export const READ_ONLY = 'This is a read-only snapshot. It cannot start, change or delete anything.'

/** The lookup key of a request; scripts/record-snapshot.mjs builds the same key. */
export function keyOf(method: string, url: URL): string {
  const q = new URLSearchParams(url.search)
  q.sort()
  const s = q.toString()
  return `${method} ${url.pathname}${s ? `?${s}` : ''}`
}

const problem = (status: number, code: Problem['code'], message: string) =>
  HttpResponse.json({ title: message, status, code, message } satisfies Problem, { status, headers: { 'Content-Type': 'application/problem+json' } })

export function snapshotHandlers(index: SnapshotIndex, load: LoadFile) {
  const bundles = new Map<string, Promise<RunBundle>>()
  const bundle = (runId: string) => {
    let b = bundles.get(runId)
    if (!b) {
      b = load<RunBundle>(`runs/${runId}.json.gz`)
      b.catch(() => bundles.delete(runId))
      bundles.set(runId, b)
    }
    return b
  }
  const runOf = (pathname: string) => {
    const id = /^\/api\/runs\/(run_[^/]+)/.exec(pathname)?.[1]
    return id && index.runs.includes(id) ? id : null
  }

  const rest = http.all('/api/*', async ({ request }) => {
    const url = new URL(request.url)
    if (request.method !== 'GET' && request.method !== 'HEAD') return problem(405, 'method_not_allowed', READ_ONLY)
    const runId = runOf(url.pathname)
    const responses = runId ? (await bundle(runId)).responses : index.responses
    const hit = responses[keyOf('GET', url)]
    if (!hit) return problem(404, 'not_found', `${url.pathname} is not in this snapshot.`)
    return HttpResponse.json(hit.body as never, { status: hit.status, headers: hit.status >= 400 ? { 'Content-Type': 'application/problem+json' } : undefined })
  })

  /** Like the backend (src/api/ws.py) for an ended run: the stored events from from_seq, then caught_up, then close. */
  const socket = ws.link(`ws://${location.host}/api/ws/runs/:runId`).addEventListener('connection', async ({ client, params }) => {
    const runId = String(params.runId)
    if (!index.runs.includes(runId)) return client.close(4404, 'run not found')
    const { events } = await bundle(runId)
    const from = Number(new URL(client.url).searchParams.get('from_seq') ?? 0)
    if (from > events.length) return client.close(4409, 'from_seq is beyond the last stored seq + 1')
    const send = (m: WsServerMessage) => client.send(JSON.stringify(m))
    for (const e of events) if (e.seq >= from) send({ type: 'event', event: e })
    send({ type: 'caught_up', last_seq: events.length - 1 })
    // MSW drops sends that are still queued when close() runs, so the close waits a little.
    setTimeout(() => client.close(1000, 'run ended'), 100)
  })

  return [rest, socket]
}
