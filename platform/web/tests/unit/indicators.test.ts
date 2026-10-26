import { describe, expect, it } from 'vitest'
import { cutAt, defaultParams, outputSpec, paramFields, type IndicatorSpec } from '@/api/indicators'
import { makeEpisode } from '@/mocks/episode'
import { computeMockIndicator, mockIndicatorSpecs } from '@/mocks/indicators'

const events = makeEpisode({ runId: 'run_I1', kind: 'research', seed: 101, rounds: 30, start: '2026-10-23T09:00:00Z', conditionId: 'C1', branch: null })
const ids = mockIndicatorSpecs.map((s) => s.id)
const params = { corr_raw: { window: 5 } }

describe('indicator series in the UI', () => {
  it('cutting the full series at t gives the same values as asking the server with until=t', () => {
    for (const id of ids) {
      const full = computeMockIndicator(id, events, params[id as keyof typeof params] ?? {}, Infinity)!
      for (const t of [1, 5, 12, 29]) {
        const server = computeMockIndicator(id, events, params[id as keyof typeof params] ?? {}, t)!
        const cut = cutAt({ run_id: 'run_I1', ...full }, t)
        expect(cut.t).toEqual(server.t)
        expect(cut.outputs).toEqual(server.outputs)
      }
    }
  })

  it('matches expanded output names to their spec template', () => {
    const spec = { ...mockIndicatorSpecs[0]!, outputs: [{ name: 'delta_p.{seller}', kind: 'line' as const, unit: 'ratio' }, { name: 'msgs', kind: 'histogram' as const, unit: 'count' }] }
    expect(outputSpec(spec, 'delta_p.A')?.kind).toBe('line')
    expect(outputSpec(spec, 'msgs')?.kind).toBe('histogram')
    expect(outputSpec(spec, 'delta_p.')).toBeUndefined()
    expect(outputSpec(spec, 'other')).toBeUndefined()
  })
})

describe('paramFields', () => {
  const spec: IndicatorSpec = {
    id: 'x',
    version: '1',
    name: 'X',
    description: '',
    inputs: [],
    data_layer: 'A',
    pane: 'separate',
    outputs: [],
    params_schema: {
      type: 'object',
      properties: {
        window: { type: 'integer', title: 'Window', minimum: 2, maximum: 20, default: 8 },
        method: { type: 'string', enum: ['pearson', 'spearman'], default: 'pearson' },
        nested: { type: 'object', properties: {} },
      },
    },
  }

  it('reads flat primitive params with their limits and defaults, and skips other shapes', () => {
    expect(paramFields(spec)).toEqual([
      { name: 'window', title: 'Window', type: 'integer', default: 8, minimum: 2, maximum: 20 },
      { name: 'method', title: 'method', type: 'string', default: 'pearson', enum: ['pearson', 'spearman'] },
    ])
    expect(defaultParams(spec)).toEqual({ window: 8, method: 'pearson' })
  })
})
