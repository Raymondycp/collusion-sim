// Mock fixtures for UI development only. Never cite these numbers.
// de's engine-made episodes (docs/sim/api/examples/) come first. The generated runs add LLM-style calls,
// a live run and a stopped run, which the examples do not have.
import { archivedRuns, resetMockArchive } from './archiveState'
import type { EpisodeHeader, RunDetail, RunSummary, SimEvent } from '@/api/types'
import { foldEvents } from '@/run/fold'
import { setAt } from '@/lib/jsonSchema'
import { makeEpisode, mockBatchId, type MockRunSpec } from './episode'
import demoRaw from '../../../docs/sim/api/examples/episode-demo.jsonl?raw'
import demoProbeRaw from '../../../docs/sim/api/examples/episode-demo-probe.jsonl?raw'

export type MockRun = {
  id: string
  header: EpisodeHeader
  events: SimEvent[]
  live: boolean
  start: string
  raw?: string
  startedAt?: number
  /** While paused, the run shows its first `count` events. */
  paused?: { time: number; count: number }
  clonedFrom?: string
  /** The mock experiment that planned this run. */
  experimentId?: string
  /** Clone overrides, JSON pointer -> value, applied to the config that detail() reports. */
  overrides?: Record<string, unknown>
}

/** Line 1 is the EpisodeHeader, each other line one SimEvent. */
export function parseEpisode(raw: string): { header: EpisodeHeader; events: SimEvent[] } {
  const [first, ...rest] = raw.split('\n').filter((l) => l.trim())
  return { header: JSON.parse(first!) as EpisodeHeader, events: rest.map((l) => JSON.parse(l) as SimEvent) }
}

function example(raw: string): MockRun {
  const { header, events } = parseEpisode(raw)
  return { id: header.run_id, header, events, live: false, start: header.created_at, raw }
}

function headerOf(events: SimEvent[]): EpisodeHeader {
  const e = events[0]
  if (e?.type !== 'run_started') throw new Error('mock episode must start with run_started')
  return e.header
}

function run(spec: MockRunSpec, opts: { live?: boolean; endAt?: number } = {}): MockRun {
  let events = makeEpisode(spec)
  if (opts.endAt !== undefined) {
    const cut = events.findIndex((e) => e.t > opts.endAt!)
    events = events.slice(0, cut)
    const last = events[events.length - 1]!
    events.push({ seq: last.seq + 1, run_id: spec.runId, t: last.t, wall_time: last.wall_time, tier: 'B', type: 'run_ended', status: 'stopped', end_reason: 'stopped_by_user', episode_hash: 'sha256:5t0p…00', cost_usd: 0.004, error: null })
  }
  return { id: spec.runId, header: headerOf(events), events, live: opts.live ?? false, start: spec.start }
}

const MAIN = 'run_MOCK01'

const initialRuns = (): MockRun[] => [
  example(demoRaw),
  example(demoProbeRaw),
  run({ runId: MAIN, kind: 'research', seed: 101, rounds: 30, start: '2026-10-23T09:00:00Z', conditionId: 'C1', branch: null }),
  run({ runId: 'run_MOCK01PROBE', kind: 'research', seed: 101, rounds: 6, start: '2026-10-23T10:00:00Z', conditionId: 'C1', branch: { parentRunId: MAIN, forkT: 20, intervention: { type: 'probe', target: 'B', price: 'nash' }, sampleIndex: 0 } }),
  run({ runId: 'run_MOCK01PLACEBO', kind: 'research', seed: 101, rounds: 6, start: '2026-10-23T10:05:00Z', conditionId: 'C1', branch: { parentRunId: MAIN, forkT: 20, intervention: { type: 'placebo' }, sampleIndex: 1 } }),
  run({ runId: 'run_MOCK02', kind: 'exploratory', seed: 7, rounds: 30, start: '2026-10-23T11:00:00Z', conditionId: null, branch: null }, { live: true }),
  run({ runId: 'run_MOCK03', kind: 'research', seed: 2, rounds: 30, start: '2026-10-22T15:00:00Z', conditionId: 'C3', split: 'pilot', branch: null, before023: true }, { endAt: 12 }),
]

export const mockRuns: MockRun[] = initialRuns()

/** Drops the runs that tests added and undoes pause and stop, so a test does not see the runs of an earlier test. */
export function resetMockRuns() {
  resetMockArchive()
  mockRuns.splice(0, mockRuns.length, ...initialRuns())
}

/** A live mock run releases one event every LIVE_MS, counted from when the mock started. */
export const LIVE_MS = 40
const startedAt = Date.now()

export function visibleEvents(r: MockRun): SimEvent[] {
  if (!r.live) return r.events
  if (r.paused) return r.events.slice(0, r.paused.count)
  const n = Math.min(r.events.length, 1 + Math.floor((Date.now() - (r.startedAt ?? startedAt)) / LIVE_MS))
  return r.events.slice(0, n)
}

/** A run started from the new-run form. It plays live from now. Returns its id. */
export function addMockRun(spec: MockRunSpec, extra: Pick<MockRun, 'clonedFrom' | 'overrides' | 'experimentId'> = {}): string {
  mockRuns.push({ ...run(spec, { live: true }), startedAt: Date.now(), ...extra })
  return spec.runId
}

export function findRun(id: string) {
  return mockRuns.find((r) => r.id === id)
}

export function summary(r: MockRun): RunSummary {
  const events = visibleEvents(r)
  const s = foldEvents(events)
  const h = s.header!
  const ended = s.status === 'completed' || s.status === 'stopped' || s.status === 'failed'
  return {
    id: r.id,
    batch_id: h.batch_id ?? mockBatchId(0),
    experiment_id: r.experimentId ?? null,
    kind: h.kind,
    name: h.branch ? null : `MOCK ${h.condition_id ?? 'exploratory'} seed ${h.seed}`,
    status: s.status ?? 'queued',
    condition_id: h.condition_id ?? null,
    labels: h.labels ?? null,
    split: h.split,
    scenario_id: h.scenario.id,
    seed: h.seed,
    call_mode: h.call_mode,
    clock_mode: h.clock.mode,
    t: s.settledT,
    max_rounds: h.max_rounds,
    branch: h.branch ?? null,
    cost_usd: Math.round(s.costUsd * 1e5) / 1e5,
    calls: s.calls,
    cache_hits: s.cacheHits,
    fallbacks: s.fallbacks,
    created_at: r.start,
    ended_at: ended ? events[events.length - 1]!.wall_time : null,
    end_reason: s.endReason,
    models: [...new Set(h.agents.flatMap((a) => (a.model ? [a.model] : [])))],
    preset_id: null,
    config_hash: `sha256:cfg${h.kind}${h.condition_id ?? ''}`,
    cloned_from: r.clonedFrom ?? null,
    archived_at: archivedRuns.get(r.id) ?? null,
  }
}

/** "/agents/0/policy/llm/temperature" -> ['agents', 0, 'policy', 'llm', 'temperature'] */
export const pointerPath = (pointer: string) =>
  pointer
    .split('/')
    .slice(1)
    .map((k) => k.replace(/~1/g, '/').replace(/~0/g, '~'))
    .map((k) => (/^\d+$/.test(k) ? Number(k) : k))

export function detail(r: MockRun): RunDetail {
  const d = baseDetail(r)
  const config = Object.entries(r.overrides ?? {}).reduce((c, [ptr, v]) => setAt(c, pointerPath(ptr), v), d.config)
  return { ...d, config }
}

function baseDetail(r: MockRun): RunDetail {
  const s = summary(r)
  const research = s.kind === 'research'
  return {
    ...s,
    scenario_hash: 'sha256:9f2c…e1',
    condition_hash: research ? 'sha256:aa01…9c' : null,
    seeds_hash: research ? 'sha256:41ab…07' : null,
    code_version: 'mock',
    snapshots: Array.from({ length: s.t ?? 0 }, (_, i) => i + 1),
    children: mockRuns.filter((c) => c.header.branch?.parent_run_id === r.id).map((c) => c.id),
    config: {
      kind: s.kind,
      name: s.name ?? undefined,
      scenario_id: s.scenario_id,
      scenario_hash: 'sha256:9f2c…e1',
      condition_id: s.condition_id,
      prompt_variant_id: research ? 'p1' : null,
      split: s.split,
      seeds: [s.seed],
      agents: ['A', 'B'].map((id) => ({
        id,
        role: 'seller',
        policy: {
          kind: 'llm' as const,
          llm: {
            provider_profile_id: 'prof_mock',
            model: 'mock-model',
            temperature: 0.7,
            system_prompt: research ? null : 'You run a petrol station. Maximise your profit.',
          },
        },
        capability: null,
      })),
      channels: research ? null : { public: true, private_pairs: true, news: true },
      transparency: research ? null : 'rival_last_round',
      call_mode: 'talk_and_act',
      clock: { mode: 'sync' },
      memory: { full_rounds: 5, summary_every: 5 },
      budget: { max_usd: 0.5 },
    },
  }
}

export type RunAction = 'pause' | 'resume' | 'stop'

/**
 * Pause, resume or stop a live mock run the way the backend does: pause and resume add `run_status_changed`,
 * stop ends the run with `run_ended` (status `stopped`). A second pause or resume changes nothing.
 * Returns false when the run has ended (the backend answers 409 run_not_active).
 */
export function controlMockRun(r: MockRun, action: RunAction): boolean {
  const shown = visibleEvents(r)
  if (!r.live || shown.length === r.events.length) return false
  const at = shown.length
  const last = shown[at - 1]!
  const base = { run_id: r.id, t: last.t, wall_time: new Date().toISOString(), tier: 'B' as const }
  const insert = (e: SimEvent) => (r.events = [...r.events.slice(0, at), e, ...r.events.slice(at).map((x) => ({ ...x, seq: x.seq + 1 }))])
  if (action === 'pause' && !r.paused) {
    insert({ ...base, seq: at, type: 'run_status_changed', status: 'paused', reason: null })
    r.paused = { time: Date.now(), count: at + 1 }
  }
  if (action === 'resume' && r.paused) {
    insert({ ...base, seq: at, type: 'run_status_changed', status: 'running', reason: null })
    r.paused = undefined
    // visibleEvents counts from startedAt: this start shows the events up to the new `running` one.
    r.startedAt = Date.now() - at * LIVE_MS
  }
  if (action === 'stop') {
    r.events = [...shown, { ...base, seq: at, type: 'run_ended', status: 'stopped', end_reason: 'stopped_by_user', episode_hash: 'sha256:5t0p…01', cost_usd: foldEvents(shown).costUsd, error: null }]
    r.live = false
    r.paused = undefined
  }
  return true
}
