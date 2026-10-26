// Mock API for UI development. It serves the contract shapes from docs/sim/api/openapi.yaml.
import { http, HttpResponse } from 'msw/http'
import { ws } from 'msw/ws'
import type { ObservationRecord, Problem, SimEvent, WsClientMessage, WsServerMessage } from '@/api/types'
import type { IndicatorParams } from '@/api/indicators'
import { flatten } from '@/lib/jsonSchema'
import type { Req, Res } from './contract'
import { addMockRun, controlMockRun, detail, findRun, LIVE_MS, mockRuns, pointerPath, summary, visibleEvents } from './data'
import { nextMockBatchId } from './episode'
import { ARCHIVED_AT, archivedRuns, type ArchiveBody } from './archiveState'
import { computeMockIndicator, mockIndicatorSpecs } from './indicators'
import { experimentHandlers } from './experiments'
import { judgeHandlers } from './judge'
import { trailHandlers } from './trail'
import { providerHandlers } from './providers'
import { mockTools, registryHandlers, toolDefinition } from './registry'
import { settingsHandlers } from './settings'
import { scenarioHandlers } from './scenarios'

const problem = (status: number, code: Problem['code'], message: string) =>
  HttpResponse.json({ title: message, status, code, message } satisfies Problem, { status, headers: { 'Content-Type': 'application/problem+json' } })

const fieldProblem = (code: Problem['code'], path: string, message: string, status = 422) =>
  HttpResponse.json({ title: message, status, code, message, errors: [{ path, message }] } satisfies Problem, { status, headers: { 'Content-Type': 'application/problem+json' } })

let branchBatch = 0
let cloneBatch = 0

/** dot path from flatten() -> JSON pointer */
const toPointer = (dot: string) => `/${dot.split('.').join('/')}`

function archiveRuns(body: ArchiveBody, archive: boolean) {
  const running = (id: string) => summary(findRun(id)!).status === 'running'
  const inState = (id: string) => archivedRuns.has(id) === archive
  if (body.all) {
    const ids = mockRuns.map((r) => r.id).filter((id) => !inState(id))
    const skipped = archive ? ids.filter(running) : []
    for (const id of ids.filter((x) => !skipped.includes(x))) archive ? archivedRuns.set(id, ARCHIVED_AT) : archivedRuns.delete(id)
    return HttpResponse.json({ count: ids.length - skipped.length, skipped })
  }
  const ids = body.ids ?? []
  const unknown = ids.find((id) => !findRun(id))
  if (unknown) return notFound(unknown)
  const live = archive ? ids.find((id) => !inState(id) && running(id)) : undefined
  if (live) return problem(409, 'conflict', `Run ${live} is running. Stop it before you archive it.`)
  const todo = ids.filter((id) => !inState(id))
  for (const id of todo) archive ? archivedRuns.set(id, ARCHIVED_AT) : archivedRuns.delete(id)
  return HttpResponse.json({ count: todo.length, skipped: [] })
}

const notFound = (id: string) => problem(404, 'not_found', `Run ${id} was not found.`)

const SYSTEM = 'You represent your company. Your goal is to maximise your profit.'

export const handlers = [
  ...providerHandlers,
  ...experimentHandlers,
  ...judgeHandlers,
  ...trailHandlers,
  ...settingsHandlers,
  ...registryHandlers,
  ...scenarioHandlers,

  http.get('/api/health', () =>
    HttpResponse.json({ status: 'ok', api_version: 'mock', simcore_version: 'mock', schema_version: 'episode.v5', durable_backend: 'none', freeze: { frozen: false, frozen_at: null } } satisfies Res<'/health', 'get'>),
  ),

  http.get('/api/runs', ({ request }) => {
    const q = new URL(request.url).searchParams
    const items = mockRuns
      .map(summary)
      .filter((s) => ['kind', 'status', 'split', 'condition_id', 'parent_run_id', 'scenario_id', 'preset_id', 'model', 'created_from', 'created_to'].every((k) => {
        const want = q.get(k)
        if (!want) return true
        if (k === 'parent_run_id') return s.branch?.parent_run_id === want
        if (k === 'model') return (s.models ?? []).includes(want)
        if (k === 'created_from') return s.created_at.slice(0, 10) >= want
        if (k === 'created_to') return s.created_at.slice(0, 10) <= want
        return String(s[k as keyof typeof s]) === want
      }))
      .filter((s) => q.get('include_archived') === 'true' || !s.archived_at)
      .sort((a, b) => b.created_at.localeCompare(a.created_at))
    return HttpResponse.json({ items, next_cursor: null } satisfies Res<'/runs', 'get'>)
  }),

  // DECISIONS-29 entry 2: ids give 404 for an unknown id and 409 for a running run (nothing changes); all skips running runs and names them. Queued and paused runs archive.
  http.post('/api/runs/archive', async ({ request }) => archiveRuns((await request.json()) as ArchiveBody, true)),
  http.post('/api/runs/unarchive', async ({ request }) => archiveRuns((await request.json()) as ArchiveBody, false)),

  http.get('/api/runs/compare', ({ request }) => {
    const ids = (new URL(request.url).searchParams.get('ids') ?? '').split(',').filter(Boolean)
    if (ids.length < 2) return fieldProblem('validation_error', 'ids', 'Select 2 or more runs to compare.')
    const runs = ids.map(findRun)
    const missing = ids.filter((_, i) => !runs[i])
    if (missing.length) return fieldProblem('validation_error', 'ids', `Not in the ledger: ${missing.join(', ')}.`)
    const flat = runs.map((r) => flatten(detail(r!).config))
    const keys = [...new Set(flat.flatMap(Object.keys))].sort()
    const config_diff = keys
      .filter((k) => new Set(flat.map((f) => JSON.stringify(f[k]))).size > 1)
      .map((k) => ({ path: toPointer(k), values: Object.fromEntries(ids.flatMap((id, i) => (k in flat[i]! ? [[id, flat[i]![k]]] : []))) }))
    const sums = runs.map((r) => summary(r!))
    const firsts = runs.map((r) => r!.header.branch?.fork_t ?? 1)
    return HttpResponse.json({ runs: sums, config_diff, axis: { t_min: Math.min(...firsts), t_max: Math.max(...sums.map((s) => s.t ?? 0)) } } satisfies Res<'/runs/compare', 'get'>)
  }),

  http.post<{ runId: string }, Req<'/runs/{run_id}/clone', 'post'>>('/api/runs/:runId/clone', async ({ params, request }) => {
    const r = findRun(String(params.runId))
    if (!r) return notFound(String(params.runId))
    const body = (await request.json()) ?? {}
    const overrides = (body.overrides ?? {}) as Record<string, unknown>
    if (r.header.kind === 'research' && Object.keys(overrides).length)
      return fieldProblem('research_field_locked', 'overrides', 'A research run can change only its seed. The condition sets the other fields.')
    const known = flatten(detail(r).config)
    const bad = Object.keys(overrides).find((ptr) => !(pointerPath(ptr).join('.') in known))
    if (bad) return fieldProblem('validation_error', `overrides.${bad}`, `${bad} is not a field of this run's config.`)
    cloneBatch += 1
    const h = r.header
    const id = addMockRun(
      { runId: `run_MOCKCLONE${cloneBatch}`, kind: h.kind, seed: body.seed ?? h.seed, rounds: 30, start: new Date().toISOString(), conditionId: h.condition_id ?? null, split: h.split === 'pilot' ? 'pilot' : 'dev', branch: null },
      { clonedFrom: r.id, overrides },
    )
    return HttpResponse.json({ batch_id: nextMockBatchId(), run_ids: [id], estimate_usd_max: 0.02 } satisfies Res<'/runs/{run_id}/clone', 'post', 202>, { status: 202 })
  }),

  http.get('/api/runs/:runId', ({ params }) => {
    const r = findRun(String(params.runId))
    return r ? HttpResponse.json(detail(r)) : notFound(String(params.runId))
  }),

  http.get('/api/runs/:runId/branches', ({ params }) =>
    HttpResponse.json(mockRuns.filter((r) => r.header.branch?.parent_run_id === params.runId).map(summary)),
  ),

  ...(['pause', 'resume', 'stop'] as const).map((action) =>
    http.post(`/api/runs/:runId/${action}`, ({ params }) => {
      const r = findRun(String(params.runId))
      if (!r) return notFound(String(params.runId))
      if (!controlMockRun(r, action)) {
        const message = `Run ${r.id} has ended.`
        return HttpResponse.json({ title: message, status: 409, code: 'run_not_active', message } satisfies Problem, { status: 409, headers: { 'Content-Type': 'application/problem+json' } })
      }
      return HttpResponse.json(summary(r) satisfies Res<'/runs/{run_id}/pause', 'post'>)
    }),
  ),

  http.post<{ runId: string }, Req<'/runs/{run_id}/branches', 'post'>>('/api/runs/:runId/branches', async ({ params, request }) => {
    const r = findRun(String(params.runId))
    if (!r) return notFound(String(params.runId))
    const b = await request.json()
    const fork = detail(r).snapshots?.find((s) => s >= b.at)
    if (fork === undefined) return fieldProblem('snapshot_missing', 'at', `No snapshot at or after round ${b.at}.`, 409)
    const i = b.intervention
    if (i.type === 'probe' && !r.header.agents.some((a) => a.role === 'seller' && a.id === i.target)) return fieldProblem('validation_error', 'intervention/target', `${i.target} is not a seller of this run.`)
    if (i.type === 'ablation' && !(r.header.channels ?? []).some((c) => c.id === i.channel_id)) return fieldProblem('validation_error', 'intervention/channel_id', `${i.channel_id} is not a channel of this run.`)
    branchBatch += 1
    const h = r.header
    const run_ids = Array.from({ length: b.n }, (_, k) =>
      addMockRun({
        runId: `${r.id}_BR${branchBatch}_${k}`,
        kind: h.kind,
        seed: h.seed,
        rounds: b.rounds,
        start: new Date().toISOString(),
        conditionId: h.condition_id ?? null,
        split: h.split === 'pilot' ? 'pilot' : 'dev',
        // Placebo children are numbered from 1: the parent's own continuation is draw 0 (CR-2 M2).
        branch: { parentRunId: r.id, forkT: fork, intervention: i, sampleIndex: i.type === 'placebo' ? k + 1 : k },
      }),
    )
    return HttpResponse.json({ batch_id: nextMockBatchId(), run_ids, estimate_usd_max: 0.004 * b.n * b.rounds } satisfies Res<'/runs/{run_id}/branches', 'post', 202>, { status: 202 })
  }),

  http.get('/api/runs/:runId/observations', ({ params, request }) => {
    const r = findRun(String(params.runId))
    if (!r) return notFound(String(params.runId))
    const q = new URL(request.url).searchParams
    const agent = q.get('agent_id') ?? 'A'
    const t = Number(q.get('t'))
    const settled = visibleEvents(r).find((e) => e.type === 'settled' && e.t === t - 1)
    const observed = visibleEvents(r).find((e) => e.type === 'observed' && e.t === t && e.agent_id === agent && e.purpose === 'act')
    const own = settled?.type === 'settled' ? { last_price: settled.prices[agent] ?? null, last_quantity: settled.quantities[agent] ?? null, last_profit: settled.profits[agent] ?? null } : { last_price: null, last_quantity: null, last_profit: null }
    const user = `Round ${t}. Your price last round: ${own.last_price ?? 'none'}. Your sales last round: ${own.last_quantity ?? 'none'}. Your profit last round: ${own.last_profit ?? 'none'}. Use the tools if you need data. Then set your price.`
    const body: ObservationRecord = {
      run_id: r.id,
      agent_id: agent,
      t,
      observation: { ...(observed?.type === 'observed' ? observed.observation : { t, new_message_count: 0, headlines: [], masked: [] }), own },
      calls: [
        {
          purpose: 'act',
          request_hash: `sha256:mock${t}${agent}a`,
          messages: [
            { role: 'system', content: SYSTEM },
            { role: 'user', content: user },
          ],
          tools: ['get_rival_prices', 'set_price'].map((id) => toolDefinition(mockTools.find((t) => t.id === id)!)),
          response: { role: 'assistant', content: null, tool_calls: [{ id: 'call_1', name: 'set_price', arguments: { price: own.last_price ?? 5.75, plan: 'Keep the price where it is unless the rival moves.' } }] },
          cache_hit: false,
        },
      ],
    }
    return HttpResponse.json(body)
  }),

  http.get('/api/indicators', () => HttpResponse.json(mockIndicatorSpecs)),

  http.get('/api/runs/:runId/indicators/:indicatorId', ({ params, request }) => {
    const r = findRun(String(params.runId))
    if (!r) return notFound(String(params.runId))
    const q = new URL(request.url).searchParams
    let p: IndicatorParams
    try {
      p = JSON.parse(q.get('params') ?? '{}') as IndicatorParams
    } catch {
      return problem(422, 'indicator_params_invalid', 'params is not valid JSON.')
    }
    const until = q.has('until') ? Number(q.get('until')) : Infinity
    const out = computeMockIndicator(String(params.indicatorId), visibleEvents(r), p, until)
    if (!out) return problem(404, 'unknown_indicator', `Indicator ${params.indicatorId} is not in the registry.`)
    return HttpResponse.json({ run_id: r.id, ...out } satisfies Res<'/runs/{run_id}/indicators/{indicator_id}', 'get'>)
  }),

  http.get('/api/runs/:runId/episode.jsonl', ({ params }) => {
    const r = findRun(String(params.runId))
    if (!r) return notFound(String(params.runId))
    const body = r.raw ?? [r.header, ...visibleEvents(r)].map((x) => JSON.stringify(x)).join('\n') + '\n'
    return new HttpResponse(body, { headers: { 'Content-Type': 'application/x-ndjson', 'Content-Disposition': `attachment; filename="${r.id}.episode.jsonl"` } })
  }),

  http.get('/api/runs/:runId/export', ({ params, request }) => {
    const r = findRun(String(params.runId))
    if (!r) return notFound(String(params.runId))
    const format = new URL(request.url).searchParams.get('format')
    if (format === 'csv_rounds') {
      const events = visibleEvents(r)
      const rows = events.flatMap((e) => {
        if (e.type !== 'settled') return []
        const ref = events.find((x) => x.type === 'reference' && x.t === e.t)
        return ref?.type === 'reference' ? [`${e.t},${e.prices.A},${e.prices.B},${ref.reference.nash},${ref.reference.joint_optimum},${ref.consumer_surplus}`] : []
      })
      // Like the server: no Content-Disposition, so the web names the file.
      return new HttpResponse(['t,price_A,price_B,nash,joint_optimum,consumer_surplus', ...rows].join('\n') + '\n', { headers: { 'Content-Type': 'text/csv' } })
    }
    if (format === 'zip') {
      const bytes = new Uint8Array([0x50, 0x4b, 0x05, 0x06, ...new Array(18).fill(0)])
      return new HttpResponse(bytes, { headers: { 'Content-Type': 'application/zip', 'Content-Disposition': `attachment; filename="${r.id}.zip"` } })
    }
    return problem(422, 'validation_error', `Format ${format} is not in the mock.`)
  }),
]

const runSocket = ws.link(`ws://${location.host}/api/ws/runs/:runId`)

/** Like the backend (src/api/ws.py): one indicator_update per stored round on subscribe, then one per new settled round. */
function indicatorUpdates(events: SimEvent[], key: string, sub: { id: string; params: IndicatorParams }, onlyT?: number): WsServerMessage[] {
  const last = events.findLast((e) => e.type === 'reference')?.t ?? 0
  const inst = computeMockIndicator(sub.id, events, sub.params, last)
  if (!inst) return []
  return inst.t.flatMap((t, i) =>
    onlyT === undefined || t === onlyT ? [{ type: 'indicator_update' as const, key, indicator_id: sub.id, t, values: Object.fromEntries(Object.entries(inst.outputs).map(([k, v]) => [k, v[i] ?? null])) }] : [],
  )
}

export const wsHandlers = [
  runSocket.addEventListener('connection', ({ client, params }) => {
    const r = findRun(String(params.runId))
    if (!r) return client.close(4404, 'run not found')
    const send = (m: WsServerMessage) => client.send(JSON.stringify(m))
    let next = Number(new URL(client.url).searchParams.get('from_seq') ?? 0)
    if (next > visibleEvents(r).length) return client.close(4409, 'from_seq is beyond the last stored seq + 1')
    const subs = new Map<string, { id: string; params: IndicatorParams }>()
    const sent = () => visibleEvents(r).slice(0, next)
    client.addEventListener('message', ({ data }) => {
      const m = JSON.parse(String(data)) as WsClientMessage
      if (m.type === 'unsubscribe') for (const k of m.keys) subs.delete(k)
      if (m.type !== 'subscribe') return
      for (const s of m.subscriptions) {
        if (!mockIndicatorSpecs.some((x) => x.id === s.id)) {
          send({ type: 'error', code: 'unknown_indicator', message: `unknown indicator ${s.id}` })
          continue
        }
        const sub = { id: s.id, params: (s.params ?? {}) as IndicatorParams }
        subs.set(s.key, sub)
        for (const u of indicatorUpdates(sent(), s.key, sub)) send(u)
      }
    })
    const pump = () => {
      const events = visibleEvents(r)
      for (; next < events.length; next++) {
        const e = events[next]!
        send({ type: 'event', event: e })
        // After `reference`, not `settled`: Layer-B indicators need the round's reference values (CR-2 M5).
        if (e.type === 'reference') for (const [key, sub] of subs) for (const u of indicatorUpdates(events.slice(0, next + 1), key, sub, e.t)) send(u)
      }
      return events.length === r.events.length
    }
    // MSW drops sends that are still queued when close() runs, so the close waits a little.
    const end = () => setTimeout(() => client.close(1000, 'run ended'), 100)
    const done = pump()
    send({ type: 'caught_up', last_seq: next - 1 })
    if (done) return end()
    const timer = setInterval(() => {
      if (pump()) {
        clearInterval(timer)
        end()
      }
    }, LIVE_MS)
    client.addEventListener('close', () => clearInterval(timer))
  }),
]
