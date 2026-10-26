// Mock experiment endpoints (contract 0.8.0). The checks follow services/experiments.py: every research check per
// condition (reused from the run mock), the batch budget, and the branch plan against each seed's last round.
import { http, HttpResponse } from 'msw/http'
import type { BranchKind, BranchPlan, CellStatus, Experiment, ExperimentCell, ExperimentCreate, ExperimentEstimate, ExperimentSummary } from '@/api/experiments'
import type { Evaluation } from '@/api/judge'
import type { Condition } from '@/api/runConfig'
import type { Problem } from '@/api/types'
import type { Res } from './contract'
import { addMockRun } from './data'
import { ARCHIVED_AT, archivedExperiments, resetMockArchive, type ArchiveBody } from './archiveState'
import { paidCallsOff } from './providers'
import { checkRunConfig, countCap, hasCap, mockResearchConditions, withinCaps } from './registry'

const problem = (status: number, code: Problem['code'], message: string, errors: Problem['errors'] = []) =>
  HttpResponse.json({ title: message, status, code, message, errors } satisfies Problem, { status, headers: { 'Content-Type': 'application/problem+json' } })

const crockford = (prefix: string, n: number) => `${prefix}01MXEX${String(n).padStart(20, '0')}`
let ids = 0
let retries = 0
const nextRunId = () => crockford('run_', ++ids)

/** Every mock seed lasts 30 rounds (the real stop round comes from each seed's scenario). */
const STOP = 30
// Worst case per LLM turn, as in the backend estimate: 4 tool steps × 3 attempts.
const CALLS_PER_TURN = 12
const PRICE = { prof_mock: [0.15, 0.6] } as Record<string, [number, number] | undefined>

type Stored = { exp: Omit<Experiment, 'cells' | 'status' | 'counts' | 'spend'>; cells: (ExperimentCell & { stepped: number; stuck?: boolean })[] }
let store: Stored[] = []
export const resetMockExperiments = () => void ((store = []), (ids = 0), (retries = 0), resetMockArchive())

const ALL_GROUPS = ['C1', 'C2a', 'C2b', 'C3', 'C4', 'LC']

/** The children of one cell: services/experiments.py `_branch_requests`, written again here so that the mock checks the UI's copy. */
function children(p: BranchPlan, c: Condition): { kind: BranchKind; at: number; rounds: number | null; n: number }[] {
  const sellers = Object.keys(c.capabilities).sort()
  const pairs = sellers.flatMap((x, i) => sellers.slice(i + 1).map(() => x))
  return [
    ...(p.probe ? [{ kind: 'probe' as const, at: p.probe.at ?? 20, rounds: p.probe.rounds ?? 5, n: 1 }] : []),
    ...(p.placebo ? [{ kind: 'placebo' as const, at: p.placebo.at ?? 20, rounds: p.placebo.rounds ?? 5, n: p.placebo.n ?? 3 }] : []),
    ...(p.ablation && (p.ablation.groups ?? ALL_GROUPS).includes(c.group) ? (c.channels.private_pairs ? pairs : [null]).map(() => ({ kind: 'ablation' as const, at: p.ablation!.at ?? 10, rounds: null, n: 1 })) : []),
  ]
}

type Bad = { status: number; code: Problem['code']; message: string; errors: Problem['errors'] }

function plan(body: ExperimentCreate): { conds: Condition[] } | Bad {
  if (!hasCap(body.batch_budget))
    return { status: 422, code: 'validation_error', message: 'The request does not match the contract.', errors: [{ path: 'batch_budget/max_usd', message: 'Value error, set at least one of max_usd, max_calls and max_tokens' }] }
  if (!hasCap(body.budget)) return { status: 422, code: 'validation_error', message: 'The request does not match the contract.', errors: [{ path: 'budget/max_usd', message: 'Value error, set at least one of max_usd, max_calls and max_tokens' }] }
  const conds: Condition[] = []
  for (const [i, cid] of body.conditions.entries()) {
    const c = mockResearchConditions().find((x) => x.id === cid)
    if (!c) return { status: 422, code: 'validation_error', message: `Condition ${cid} was not found.`, errors: [{ path: `conditions/${i}`, message: 'unknown condition' }] }
    const bad = checkRunConfig({ kind: 'research', condition_id: cid, split: body.split, seeds: body.seeds, agents: body.agents_by_condition?.[cid] ?? body.agents, clock: body.clock ?? { mode: 'sync' }, memory: body.memory, budget: body.budget })
    if (bad) return { status: bad.status ?? 422, code: bad.code, message: bad.message, errors: bad.errors }
    conds.push(c)
  }
  const errors = conds.flatMap((c) =>
    children(body.branch_plan ?? {}, c)
      .filter((k) => k.rounds !== null && k.at + k.rounds - 1 > STOP)
      .map((k) => ({ path: `branch_plan/${k.kind}`, message: `${c.id}: rounds ${k.at}–${k.at + k.rounds! - 1} pass the last round ${STOP}` })),
  )
  const target = body.branch_plan?.probe?.target
  if (body.branch_plan?.probe && conds.some((c) => !(body.agents_by_condition?.[c.id] ?? body.agents).some((a) => a.role === 'seller' && a.id === target)))
    errors.push({ path: 'branch_plan/probe/target', message: 'not a seller' })
  if (errors.length) return { status: 422, code: 'validation_error', message: 'The branch plan does not fit every cell.', errors }
  return { conds }
}

function estimate(body: ExperimentCreate, conds: Condition[]): ExperimentEstimate {
  const runs = { parents: 0, probe: 0, placebo: 0, ablation: 0 }
  const missing = new Set<string>()
  const per = conds.map((c) => {
    const llm = (body.agents_by_condition?.[c.id] ?? body.agents).filter((a) => a.role === 'seller' && a.policy.kind === 'llm')
    const turns = c.call_mode === 'talk_and_act' ? 2 : 1
    let rounds = 0
    let n = 0
    for (const _ of body.seeds) {
      runs.parents += 1
      rounds += STOP
      n += 1
      for (const k of children(body.branch_plan ?? {}, c)) {
        runs[k.kind] += k.n
        n += k.n
        rounds += k.n * (k.rounds ?? STOP - k.at + 1)
      }
    }
    const calls = llm.length * rounds * turns * CALLS_PER_TURN
    const input = calls * 2500
    const output = calls * (llm[0]?.policy.llm?.max_output_tokens ?? 400)
    let usd: number | null = 0
    for (const a of llm) {
      const price = PRICE[a.policy.llm!.provider_profile_id]
      if (!price) missing.add(a.policy.llm!.provider_profile_id)
      usd = price && usd !== null ? usd + ((price[0] * input + price[1] * output) / 1e6) / llm.length : null
    }
    return { condition_id: c.id, runs: n, calls_max: calls, input_tokens_max: input, output_tokens_max: output, usd_max: usd === null ? null : Math.round(usd * 1e6) / 1e6 }
  })
  const usd = per.some((p) => p.usd_max === null) ? null : Math.round(per.reduce((s, p) => s + p.usd_max!, 0) * 1e6) / 1e6
  const calls = per.reduce((s, p) => s + p.calls_max, 0)
  const tokens = per.reduce((s, p) => s + p.input_tokens_max + p.output_tokens_max, 0)
  return {
    n_cells: conds.length * body.seeds.length,
    runs: { ...runs, total: runs.parents + runs.probe + runs.placebo + runs.ablation },
    calls_max: calls,
    tokens_max: { input: per.reduce((s, p) => s + p.input_tokens_max, 0), output: per.reduce((s, p) => s + p.output_tokens_max, 0) },
    usd_max: usd,
    pricing_missing_for: [...missing].sort(),
    per_condition: per,
    within_budget: withinCaps(body.batch_budget, { calls, tokens, usd }),
    rate_window: [],
    warnings: missing.size && !countCap(body.batch_budget) ? [`Profiles ${JSON.stringify([...missing].sort())} have no price and the batch budget has only max_usd: the start is refused (budget_unenforceable).`] : [],
    assumptions: ['Mock estimate: every seed lasts 30 rounds and every LLM turn uses all tool steps and attempts.'],
  }
}

/**
 * Two completed main judge jobs per experiment with a completed parent: one with the LLM backend, one scripted only
 * (CR-67 n1). Their ids come from the experiment id.
 */
function evaluationsOf(s: Stored): Evaluation[] {
  const run_ids = s.cells.filter((c) => c.parent.status === 'completed').map((c) => c.parent.run_id)
  if (!run_ids.length) return []
  const at = '2026-10-23T12:00:00Z'
  const llm: Evaluation = {
    id: s.exp.id.replace('exp_', 'eval_'),
    status: 'completed',
    block: 'main',
    view: 'talk_only',
    item_set: { id: 'items-v1', hash: 'sha256:mockrubric01' },
    backends: { llm: { provider_profile_id: 'prof_mock', model: 'mock-judge', model_family: 'mockfamily' } },
    order_seed: 1,
    run_ids,
    skipped: [],
    created_at: at,
    started_at: at,
    finished_at: at,
    per_backend: {},
    error: null,
  }
  return [llm, { ...llm, id: s.exp.id.replace('exp_01MXEX', 'eval_01MXSC'), backends: { scripted: {} } }]
}

// Each GET of an experiment moves every live cell one step: queued → running → branching → completed.
const STEPS: CellStatus[] = ['queued', 'running', 'branching', 'completed']
const parentAt = (step: number) => (['queued', 'running', 'completed', 'completed'] as const)[step]!
const childAt = (step: number) => (['planned', 'planned', 'running', 'completed'] as const)[step]!

function step(cell: Stored['cells'][number]) {
  if (cell.error || cell.stepped >= STEPS.length - 1) return
  cell.stepped += 1
  cell.status = STEPS[cell.stepped]!
  cell.parent = { ...cell.parent, status: parentAt(cell.stepped) }
  cell.branches = cell.branches.map((b) => ({ ...b, status: childAt(cell.stepped) }))
}

const cellOut = ({ stepped: _s, ...cell }: Stored['cells'][number]): ExperimentCell => ({ ...cell, status: cell.error ? 'blocked' : cell.status })

function view(s: Stored): Experiment {
  const cells = s.cells.map(cellOut)
  const counts: Record<string, number> = {}
  for (const c of cells) counts[c.status] = (counts[c.status] ?? 0) + 1
  const live = Object.keys(counts).filter((k) => !['completed', 'stopped', 'failed', 'blocked'].includes(k))
  const status = live.length ? 'running' : counts.blocked ? 'blocked' : Object.keys(counts).every((k) => k === 'completed') ? 'completed' : 'ended'
  const done = s.cells.reduce((n, c) => n + c.stepped, 0)
  const unpriced = s.exp.estimate.pricing_missing_for.length ? done * 10 : 0
  return { ...s.exp, cells, counts, status, spend: { calls: done * 40, usd: Math.round(done * 0.0123 * 1e4) / 1e4, tokens: done * 40 * 2200, unpriced, usd_complete: unpriced === 0 } }
}

const summaryOf = ({ cells: _c, branch_plan: _p, batch_budget: _b, estimate: _e, ...rest }: Experiment): ExperimentSummary => ({ ...rest, archived_at: archivedExperiments.get(rest.id) ?? null })

/** Test switch: the server could not start this cell's next run (the backend's `advance` error). */
export function blockMockCell(expId: string, conditionId: string, seed: number, code: string, message: string, opts: { stuck?: boolean } = {}) {
  const cell = store.find((s) => s.exp.id === expId)?.cells.find((c) => c.condition_id === conditionId && c.seed === seed)
  if (cell) Object.assign(cell, { error: { code, message, at: '2026-10-21T12:00:00Z' }, stuck: opts.stuck })
}

/** DECISIONS-29 entry 2: a running experiment gives 409 by ids and is skipped with all. An unknown id is 404 and nothing changes. */
function archiveExperiments(body: ArchiveBody, archive: boolean) {
  const inState = (id: string) => archivedExperiments.has(id) === archive
  const running = (id: string) => view(store.find((s) => s.exp.id === id)!).status === 'running'
  const ids = body.all ? store.map((s) => s.exp.id).filter((id) => !inState(id)) : (body.ids ?? [])
  const unknown = ids.find((id) => !store.some((s) => s.exp.id === id))
  if (unknown) return problem(404, 'not_found', `Experiment ${unknown} not found.`)
  const skipped = archive && body.all ? ids.filter(running) : []
  const live = archive && !body.all ? ids.find((id) => !inState(id) && running(id)) : undefined
  if (live) return problem(409, 'conflict', `Experiment ${live} is running. Stop it before you archive it.`)
  const todo = ids.filter((id) => !inState(id) && !skipped.includes(id))
  for (const id of todo) archive ? archivedExperiments.set(id, ARCHIVED_AT) : archivedExperiments.delete(id)
  return HttpResponse.json({ count: todo.length, skipped })
}

export const experimentHandlers = [
  http.post('/api/experiments/estimate', async ({ request }) => {
    const body = (await request.json()) as ExperimentCreate
    const p = plan(body)
    return 'conds' in p ? HttpResponse.json(estimate(body, p.conds) satisfies Res<'/experiments/estimate', 'post'>) : problem(p.status, p.code, p.message, p.errors)
  }),
  http.post('/api/experiments', async ({ request }) => {
    const body = (await request.json()) as ExperimentCreate
    const p = plan(body)
    if (!('conds' in p)) return problem(p.status, p.code, p.message, p.errors)
    const llm = [body.agents, ...Object.values(body.agents_by_condition ?? {})].flat().some((a) => a.policy.kind === 'llm')
    const est = estimate(body, p.conds)
    if (est.pricing_missing_for.length && !countCap(body.batch_budget))
      return problem(422, 'budget_unenforceable', `Profiles ${JSON.stringify(est.pricing_missing_for)} have no price, so batch_budget.max_usd cannot stop their calls.`, [{ path: 'batch_budget/max_calls', message: 'required when a profile has no price' }])
    const off = llm ? paidCallsOff() : null
    if (off) return off
    const id = crockford('exp_', store.length + 1)
    const cells = p.conds.flatMap((c) =>
      body.seeds.map((seed) => {
        const parent = nextRunId()
        addMockRun({ runId: parent, kind: 'research', seed, rounds: STOP, start: new Date().toISOString(), conditionId: c.id, split: body.split === 'pilot' ? 'pilot' : 'dev', branch: null }, { experimentId: id })
        const branches = children(body.branch_plan ?? {}, c).flatMap((k, pos) =>
          Array.from({ length: k.n }, (_, j) => ({ run_id: nextRunId(), status: 'planned' as const, end_reason: null, kind: k.kind, index: pos * 10 + j })),
        )
        return { condition_id: c.id, seed, status: 'queued' as CellStatus, error: null, parent: { run_id: parent, status: 'queued' as const, end_reason: null }, branches, stepped: 0 }
      }),
    )
    const s: Stored = {
      exp: {
        id,
        name: body.name ?? null,
        hash: `sha256:mockexp${store.length + 1}`,
        created_at: new Date().toISOString(),
        split: body.split,
        conditions: body.conditions,
        seeds: body.seeds,
        branch_plan: body.branch_plan ?? {},
        batch_budget: body.batch_budget,
        estimate: estimate(body, p.conds),
      },
      cells,
    }
    store = [s, ...store]
    return HttpResponse.json(view(s) satisfies Res<'/experiments', 'post', 202>, { status: 202 })
  }),
  http.get('/api/experiments', ({ request }) => {
    const all = new URL(request.url).searchParams.get('include_archived') === 'true'
    return HttpResponse.json(store.map((s) => summaryOf(view(s))).filter((x) => all || !x.archived_at) satisfies Res<'/experiments', 'get'>)
  }),
  http.post('/api/experiments/archive', async ({ request }) => archiveExperiments((await request.json()) as ArchiveBody, true)),
  http.post('/api/experiments/unarchive', async ({ request }) => archiveExperiments((await request.json()) as ArchiveBody, false)),
  http.get('/api/experiments/:id', ({ params }) => {
    const s = store.find((x) => x.exp.id === params.id)
    if (!s) return problem(404, 'not_found', `Experiment ${params.id} not found.`)
    const out = view(s)
    s.cells.forEach(step)
    return HttpResponse.json(out satisfies Res<'/experiments/{experiment_id}', 'get'>)
  }),
  http.get('/api/evaluations', ({ request }) => {
    const status = new URL(request.url).searchParams.get('status')
    const out = store.flatMap(evaluationsOf).filter((e) => !status || e.status === status)
    return HttpResponse.json(out satisfies Res<'/evaluations', 'get'>)
  }),
  // Like services/features.py: one row per cell, test rows locked (409), csv without Content-Disposition. No feature columns in the
  // mock. With evaluation_id: judge_evaluation_id per row and meta.judge; an evaluation of another experiment is 404 here, a job with no llm backend 422.
  http.get('/api/experiments/:id/features', ({ params, request }) => {
    const s = store.find((x) => x.exp.id === params.id)
    if (!s) return problem(404, 'not_found', `Experiment ${params.id} not found.`)
    if (s.exp.split === 'test') return problem(409, 'test_split_locked', 'Rows of test seeds need a valid freeze.')
    const q = new URL(request.url).searchParams
    const layer = q.get('layer') === 'A' ? 'A' : 'B'
    const asked = [...new Set(q.getAll('evaluation_id'))]
    const jobs = evaluationsOf(s)
    const bad = asked.find((id) => !jobs.some((e) => e.id === id))
    if (bad) return problem(404, 'not_found', `Evaluation ${bad} not found.`)
    const noLlm = asked.findIndex((id) => !jobs.find((e) => e.id === id)!.backends.llm)
    if (noLlm >= 0) return problem(422, 'validation_error', 'The evaluations cannot give judge columns.', [{ path: `evaluation_id/${noLlm}`, message: 'no llm backend' }])
    const ev = jobs.find((e) => e.backends.llm)
    const judgedBy = (run: string) => (ev && asked.length && ev.run_ids.includes(run) ? ev.id : null)
    const columns = ['experiment_id', 'condition_id', 'split', 'seed', 'parent_run_id', ...(asked.length ? (['judge_evaluation_id'] as const) : [])]
    const rows = s.cells.map((c) => ({ experiment_id: s.exp.id, condition_id: c.condition_id, split: s.exp.split, seed: c.seed, parent_run_id: c.parent.run_id, ...(asked.length ? { judge_evaluation_id: judgedBy(c.parent.run_id) } : {}) }))
    if (q.get('format') === 'csv')
      return new HttpResponse([columns.join(','), ...rows.map((r) => columns.map((k) => (r as Record<string, unknown>)[k] ?? '').join(','))].join('\n') + '\n', { headers: { 'Content-Type': 'text/csv' } })
    const judge = ev && asked.length
      ? {
          evaluations: [{ evaluation_id: ev.id, status: ev.status, block: ev.block, episodes: ev.run_ids.length }],
          view: ev.view,
          transform: 'none',
          item_set: ev.item_set,
          backend: 'llm' as const,
          model: 'mock-judge',
          model_family: 'mockfamily',
          wordings_by_group: { C1: [3] },
          invalid_rate_by_group: { C1: 0 },
        }
      : undefined
    return HttpResponse.json({ meta: { experiment_id: s.exp.id, features_code_hash: 'sha256:mockfeatures01', layer, features: [], ...(judge ? { judge } : {}) }, columns: [...columns], rows } satisfies Res<'/experiments/{experiment_id}/features', 'get'>)
  }),
  // A cell whose cause is still there (`stuck`) is blocked again with a new time; the others move on.
  http.post('/api/experiments/:id/retry', ({ params }) => {
    const s = store.find((x) => x.exp.id === params.id)
    if (!s) return problem(404, 'not_found', `Experiment ${params.id} not found.`)
    retries += 1
    for (const c of s.cells) if (c.error) c.error = c.stuck ? { ...c.error, at: `2026-10-21T12:${String(retries).padStart(2, '0')}:00Z` } : null
    return HttpResponse.json(view(s) satisfies Res<'/experiments/{experiment_id}/retry', 'post'>)
  }),
]
