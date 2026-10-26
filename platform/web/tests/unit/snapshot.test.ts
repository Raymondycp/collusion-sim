import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { setupServer } from 'msw/node'
import type { SimEvent } from '@/api/types'
import { makeEpisode } from '@/mocks/episode'
import { READ_ONLY, snapshotHandlers, type RunBundle, type SnapshotIndex } from '@/mocks/snapshot'

const RUN = 'run_01SNAPSHOT0000000000000001'
const events = makeEpisode({ runId: RUN, kind: 'exploratory', seed: 1, rounds: 3, start: '2026-10-26T09:00:00Z', conditionId: null, branch: null }) as SimEvent[]
const index: SnapshotIndex = {
  recorded_at: '2026-10-26T09:00:00Z',
  source: 'http://127.0.0.1:8018',
  api_version: '0.45.0',
  experiments: ['exp_1'],
  runs: [RUN],
  responses: {
    'GET /api/runs?limit=200': { status: 200, body: { items: [{ id: RUN }], next_cursor: null } },
    'GET /api/runs?limit=200&status=completed': { status: 200, body: { items: [], next_cursor: null } },
  },
}
const bundle: RunBundle = {
  responses: {
    [`GET /api/runs/${RUN}`]: { status: 200, body: { id: RUN, status: 'completed' } },
    [`GET /api/runs/${RUN}/indicators/delta_p?params=%7B%22smooth%22%3A1%7D`]: { status: 200, body: { indicator_id: 'delta_p' } },
    [`GET /api/runs/${RUN}/calls/9/trail`]: { status: 404, body: { title: 'x', status: 404, code: 'not_found', message: 'no call 9' } },
  },
  events,
}
const loads: string[] = []
const load = async <T,>(name: string) => {
  loads.push(name)
  if (name === `runs/${RUN}.json.gz`) return structuredClone(bundle) as T
  throw new Error(`no file ${name}`)
}

const server = setupServer(...snapshotHandlers(index, load))
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

const api = (path: string, init?: RequestInit) => fetch(`${location.origin}${path}`, init)

/** Every frame of one replay socket, until the server closes it. */
function replay(fromSeq: number) {
  return new Promise<{ frames: { type: string; event?: SimEvent; last_seq?: number }[]; code: number }>((resolve) => {
    const s = new WebSocket(`ws://${location.host}/api/ws/runs/${RUN}?from_seq=${fromSeq}`)
    const frames: { type: string }[] = []
    s.onmessage = (m) => frames.push(JSON.parse(String(m.data)))
    s.onclose = (e) => resolve({ frames, code: e.code })
  })
}

describe('read-only snapshot handlers', () => {
  it('answers a recorded GET whatever the query order, and the run answers from its own file', async () => {
    expect(await (await api('/api/runs?status=completed&limit=200')).json()).toEqual({ items: [], next_cursor: null })
    expect(await (await api('/api/runs?limit=200')).json()).toEqual({ items: [{ id: RUN }], next_cursor: null })
    const r = await api(`/api/runs/${RUN}/indicators/delta_p?params=${encodeURIComponent('{"smooth":1}')}`)
    expect(r.status).toBe(200)
    expect(await r.json()).toEqual({ indicator_id: 'delta_p' })
    expect(await (await api(`/api/runs/${RUN}`)).json()).toEqual({ id: RUN, status: 'completed' })
    expect(loads.filter((n) => n.startsWith('runs/'))).toEqual([`runs/${RUN}.json.gz`])
  })

  it('keeps a recorded error status, and answers 404 for a request that is not in the snapshot', async () => {
    const trail = await api(`/api/runs/${RUN}/calls/9/trail`)
    expect(trail.status).toBe(404)
    expect((await trail.json()).message).toBe('no call 9')
    const other = await api('/api/provider-profiles')
    expect(other.status).toBe(404)
    expect((await other.json()).code).toBe('not_found')
    expect((await api('/api/runs/run_NOT_IN_SCOPE')).status).toBe(404)
  })

  it('refuses every write with 405', async () => {
    for (const [method, path] of [['POST', '/api/runs'], ['POST', '/api/runs/archive'], ['PUT', '/api/defaults'], ['DELETE', `/api/runs/${RUN}`], ['PATCH', '/api/scenarios/s']]) {
      const r = await api(path!, { method, body: method === 'DELETE' ? undefined : '{}', headers: { 'Content-Type': 'application/json' } })
      expect(r.status, `${method} ${path}`).toBe(405)
      expect((await r.json()).message).toBe(READ_ONLY)
    }
  })

  it('replays the stored events from from_seq, then caught_up, then closes', async () => {
    const all = await replay(0)
    const sent = all.frames.filter((f) => f.type === 'event').map((f) => f.event!.seq)
    expect(sent).toEqual(events.map((e) => e.seq))
    expect(all.frames.at(-1)).toEqual({ type: 'caught_up', last_seq: events.length - 1 })
    expect(all.code).toBe(1000)
    const tail = await replay(events.length - 2)
    expect(tail.frames.filter((f) => f.type === 'event').map((f) => f.event!.seq)).toEqual([events.length - 2, events.length - 1])
  })
})
