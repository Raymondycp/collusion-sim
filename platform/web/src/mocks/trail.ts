// Mock trail route: se's example trail (docs/sim/api/examples/trail-demo.jsonl) is the shape, with the mock run's llm_call fields on it.
import { http, HttpResponse } from 'msw/http'
import type { Problem } from '@/api/types'
import type { TrailLine } from '@/api/trail'
import { findRun, visibleEvents } from './data'
import demoRaw from '../../../docs/sim/api/examples/trail-demo.jsonl?raw'

export const trailDemo = demoRaw
  .split('\n')
  .filter((l) => l.trim())
  .map((l) => JSON.parse(l) as TrailLine)

/** A failed call has no cache row; a talk call answers with text, a pricing call with a tool call. */
const template = (purpose: TrailLine['purpose'], failed: boolean) =>
  trailDemo.find((l) => (failed ? l.missing === 'no_cache_row' : !l.missing && (purpose === 'act' ? l.purpose === 'act' : l.purpose === 'talk')))!

const problem = (status: number, code: Problem['code'], message: string) =>
  HttpResponse.json({ title: message, status, code, message } satisfies Problem, { status, headers: { 'Content-Type': 'application/problem+json' } })

export const trailHandlers = [
  http.get('/api/runs/:runId/calls/:seq/trail', ({ params }) => {
    const r = findRun(String(params.runId))
    const e = r && visibleEvents(r).find((x) => x.type === 'llm_call' && x.seq === Number(params.seq))
    if (!r || e?.type !== 'llm_call') return problem(404, 'not_found', `Run ${String(params.runId)} has no llm_call with seq ${String(params.seq)}.`)
    const { type: _type, tier: _tier, ...fields } = e
    return HttpResponse.json({ ...template(e.purpose, !!e.error), ...fields, run_id: r.id } satisfies TrailLine)
  }),
]
