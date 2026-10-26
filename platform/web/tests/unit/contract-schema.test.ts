import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import Ajv2020 from 'ajv/dist/2020'
import { load } from 'js-yaml'
import demoRaw from '../../../docs/sim/api/examples/episode-demo.jsonl?raw'
import probeRaw from '../../../docs/sim/api/examples/episode-demo-probe.jsonl?raw'
import trailRaw from '../../../docs/sim/api/examples/trail-demo.jsonl?raw'
import { makeEpisode, nextMockBatchId } from '@/mocks/episode'
import { mockRuns, summary } from '@/mocks/data'

// OpenAPI 3.1 schemas are JSON Schema 2020-12. `strict: false` lets Ajv skip OpenAPI-only keywords (`example`, `discriminator`).
const ajv = new Ajv2020({ strict: false, allErrors: true, validateFormats: false })
// Read from disk: the dev server serves only the examples folder of docs/, and the contract need not be served.
ajv.addSchema(load(readFileSync(resolve(__dirname, '../../../docs/sim/api/openapi.yaml'), 'utf8')) as object, 'openapi')
const header = ajv.getSchema('openapi#/components/schemas/EpisodeHeader')!
const event = ajv.getSchema('openapi#/components/schemas/SimEvent')!

function check(lines: unknown[]) {
  const [h, ...events] = lines
  const errors: string[] = []
  if (!header(h)) errors.push(`header: ${ajv.errorsText(header.errors)}`)
  for (const e of events) if (!event(e)) errors.push(`seq ${(e as { seq: number }).seq}: ${ajv.errorsText(event.errors)}`)
  return errors
}

const jsonl = (raw: string) => raw.trim().split('\n').map((l) => JSON.parse(l) as unknown)

describe('episodes against the contract schemas (m26)', () => {
  it.each([
    ['episode-demo', demoRaw],
    ['episode-demo-probe', probeRaw],
  ])("de's example %s: the header and every event match the schemas", (_, raw) => {
    expect(check(jsonl(raw))).toEqual([])
  })

  it("se's trail-demo.jsonl: every line matches TrailLine, and one line has no cache row (0.28.0)", () => {
    const line = ajv.getSchema('openapi#/components/schemas/TrailLine')!
    const lines = jsonl(trailRaw)
    expect(lines.flatMap((l) => (line(l) ? [] : [`seq ${(l as { seq: number }).seq}: ${ajv.errorsText(line.errors)}`]))).toEqual([])
    expect(lines.filter((l) => (l as { missing: string | null }).missing === 'no_cache_row')).toHaveLength(1)
  })

  it.each([
    ['main run', 'C1', null, false],
    ['ablation branch', 'C1', { parentRunId: 'run_M', forkT: 10, intervention: { type: 'ablation', channel_id: 'public', mode: 'clear' }, sampleIndex: 0 }, false],
    ['shuffle branch', 'C1', { parentRunId: 'run_M', forkT: 10, intervention: { type: 'ablation', channel_id: 'private_A_B', mode: 'shuffle' }, sampleIndex: 0 }, false],
    ['C3-silent run (0.23.0)', 'C3-silent', null, false],
    ['run stored before 0.23.0', 'C3', null, true],
  ] as const)("the mock's %s matches the schemas", (_, conditionId, branch, before023) => {
    const events = makeEpisode({ runId: 'run_M', kind: 'research', seed: 101, rounds: 12, start: '2026-10-23T09:00:00Z', conditionId, branch, before023 })
    const started = events[0]!
    if (started.type !== 'run_started') throw new Error('the mock episode must open with run_started')
    expect(check([started.header, ...events])).toEqual([])
  })

  it('the mock is episode.v5: a talk_replied before each post, and the observation holds rival prices and delivered posts (0.18.0)', () => {
    const run = (conditionId: string) => makeEpisode({ runId: 'run_M', kind: 'research', seed: 101, rounds: 13, start: '2026-10-23T09:00:00Z', conditionId, branch: null })
    const events = run('C1')
    const started = events[0]!
    expect(started.type === 'run_started' && started.header.schema_version).toBe('episode.v5')
    // 0.22.1: a successful call has no HTTP status and no provider code.
    const calls = events.filter((e) => e.type === 'llm_call')
    expect(calls.length).toBeGreaterThan(0)
    expect(calls.every((e) => e.http_status === null && e.provider_code === null)).toBe(true)
    events.forEach((e, i) => e.type === 'posted' && expect(events[i - 1]).toMatchObject({ type: 'talk_replied', agent_id: e.agent_id, outcome: 'posted', text: e.text }))
    const seen = (evs: typeof events, t: number, agent: string, purpose = 'talk') => evs.find((e) => e.type === 'observed' && e.t === t && e.agent_id === agent && e.purpose === purpose)
    // A posted in private_A_B in round 2; B gets it in round 3.
    expect(seen(events, 3, 'B')).toMatchObject({ observation: { new_message_count: 1, new_messages: [{ channel_id: 'private_A_B', sender: 'A' }] } })
    expect(seen(events, 3, 'A')).toMatchObject({ observation: { new_message_count: 0, new_messages: [] } })
    expect(Object.keys((seen(events, 3, 'A') as { observation: { rival_last_prices: object } }).observation.rival_last_prices)).toEqual(['B'])
    expect(seen(events, 1, 'A')).toMatchObject({ observation: { rival_last_prices: {} } })
    expect(seen(events, 3, 'B', 'act')).toMatchObject({ observation: { new_message_count: 1, new_messages: [{ sender: 'A' }] } })
    // C3 isolation (openapi c3_isolation, read_channel): the talk call still reads the private post; the act call sees no message, public or private.
    const c3 = run('C3')
    expect(seen(c3, 3, 'B')).toMatchObject({ observation: { new_message_count: 1, new_messages: [{ channel_id: 'private_A_B', sender: 'A' }] } })
    expect(seen(c3, 3, 'B', 'act')).toMatchObject({ observation: { new_message_count: 0, new_messages: [] } })
    expect(seen(c3, 13, 'A', 'act')).toMatchObject({ observation: { new_message_count: 0, new_messages: [] } })
    expect(seen(c3, 13, 'A')).toMatchObject({ observation: { new_messages: [{ channel_id: 'public', sender: 'B' }] } })
  })

  it('accepts null rival_last_prices and new_messages: an episode stored before 0.18.0 (CR-46 n6)', () => {
    const observed = jsonl(demoRaw).find((e) => (e as { type: string }).type === 'observed') as { observation: object }
    expect(event({ ...observed, observation: { ...observed.observation, rival_last_prices: null, new_messages: null } })).toBe(true)
  })

  it('rejects an extra field on an event, such as the old acted.plan (CR-2 m7)', () => {
    const acted = jsonl(demoRaw).find((e) => (e as { type: string }).type === 'acted') as Record<string, unknown>
    expect(event(acted)).toBe(true)
    expect(event({ ...acted, plan: 'Keep the price.' })).toBe(false)
  })

  it('rejects an event that breaks the schema, so the check is not empty', () => {
    expect(event({ seq: 0, run_id: 'run_M', type: 'settled', t: 1, tier: 'A' })).toBe(false)
  })
})

describe('mock REST answers against the contract schemas', () => {
  const runSummary = ajv.getSchema('openapi#/components/schemas/RunSummary')!
  const runStart = ajv.getSchema('openapi#/components/schemas/RunStartResponse')!

  it('every run summary that the mock builds from its own episodes matches RunSummary', () => {
    // Runs read from de's example episodes (`raw`) carry de's ids; only the mock's own ids are checked here.
    const own = mockRuns.filter((r) => !r.raw)
    expect(own.length).toBeGreaterThan(0)
    const bad = own.flatMap((r) => (runSummary(summary(r)) ? [] : [`${r.id}: ${ajv.errorsText(runSummary.errors)}`]))
    expect(bad).toEqual([])
  })

  it('a batch id from a mock start, branch or clone matches RunStartResponse', () => {
    expect(runStart({ batch_id: nextMockBatchId(), run_ids: ['run_MOCKNEW1_0'], estimate_usd_max: 0 })).toBe(true)
    expect(runStart({ batch_id: 'bat_MOCK', run_ids: ['run_MOCKNEW1_0'], estimate_usd_max: 0 })).toBe(false)
  })
})
