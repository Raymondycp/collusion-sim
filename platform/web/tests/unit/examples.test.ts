import { describe, expect, it } from 'vitest'
import type { SimEventType } from '@/api/types'
import { foldEvents, viewAt } from '@/run/fold'
import { parseEpisode } from '@/mocks/data'
import demoRaw from '../../../docs/sim/api/examples/episode-demo.jsonl?raw'
import probeRaw from '../../../docs/sim/api/examples/episode-demo-probe.jsonl?raw'

const KNOWN: SimEventType[] = [
  'run_started', 'round_started', 'news_published', 'observed', 'llm_call', 'tool_called', 'posted', 'talk_replied', 'talk_text', 'planned', 'acted',
  'settled', 'reference', 'outcome', 'memory_summarised', 'intervened', 'snapshot_saved', 'budget_warning', 'run_status_changed', 'run_ended',
]

const demo = parseEpisode(demoRaw)
const probe = parseEpisode(probeRaw)

describe("de's engine-made example episodes (contract test)", () => {
  it.each([
    ['episode-demo', demo],
    ['episode-demo-probe', probe],
  ])('%s: seq runs 0..n with no gap, and every event type is one the UI knows', (_, ep) => {
    expect(ep.events.map((e) => e.seq)).toEqual(ep.events.map((_, i) => i))
    for (const e of ep.events) expect(KNOWN).toContain(e.type)
    const s = foldEvents(ep.events)
    expect(s.gapFrom).toBeNull()
    expect(s.lastSeq).toBe(ep.events.length - 1)
    expect(s.header?.run_id).toBe(ep.header.run_id)
  })

  it('demo: two sellers, 30 settled rounds, one news item, one post per round, reference prices from the events', () => {
    const s = foldEvents(demo.events)
    expect(s.sellers).toEqual(['A', 'B'])
    expect(s.prices.A).toHaveLength(30)
    expect(s.reference).toHaveLength(30)
    expect(s.reference[0]).toMatchObject({ t: 1, nash: 5.75, joint_optimum: 10.5 })
    expect(s.news.map((n) => n.t)).toEqual([8])
    expect(s.messages).toHaveLength(30)
    expect(s.status).toBe('completed')
  })

  it('probe: the child starts at fork_t, A is forced to Nash, B punishes in rounds 16-18 and returns to 10.5', () => {
    const s = foldEvents(probe.events)
    expect(s.header?.branch).toMatchObject({ parent_run_id: demo.header.run_id, fork_t: 15 })
    expect(s.prices.A?.[0]).toEqual({ t: 15, value: 5.75 })
    expect(s.markers.find((m) => m.kind === 'probe')).toMatchObject({ t: 15, agentId: 'A' })
    const b = Object.fromEntries((s.prices.B ?? []).map((p) => [p.t, p.value]))
    for (const t of [16, 17, 18]) expect(b[t]).toBeLessThan(10.5)
    expect(b[19]).toBe(10.5)
  })

  it('seek: viewAt(t) on the demo equals folding the events up to t', () => {
    const full = foldEvents(demo.events)
    for (const t of [1, 8, 12, 29]) {
      expect(viewAt(full, t).prices).toEqual(foldEvents(demo.events.filter((e) => e.t <= t)).prices)
    }
  })
})
