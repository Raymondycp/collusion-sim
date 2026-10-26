import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { StrictMode } from 'react'
import { PriceChart } from '@/components/chart/PriceChart'
import { applyEvents, emptyRunState, type RunState } from '@/run/fold'
import { makeEpisode } from '@/mocks/episode'

// jsdom has no canvas, so the chart library is replaced by a recorder of the calls the component makes.
const calls = vi.hoisted(() => ({ created: 0, removed: 0, fits: [] as number[], lines: [] as Record<string, unknown>[] }))
vi.mock('lightweight-charts', () => {
  const series = (opts: Record<string, unknown>) => {
    calls.lines.push(opts)
    return { setData() {}, applyOptions() {}, seriesType: () => 'Line', getPane: () => ({ paneIndex: () => 0 }), moveToPane() {} }
  }
  return {
    ColorType: { Solid: 'solid' },
    LineStyle: { Solid: 0, Dotted: 1, Dashed: 2, LargeDashed: 3, SparseDotted: 4 },
    LineType: { Simple: 0, WithSteps: 1 },
    LineSeries: 'Line',
    HistogramSeries: 'Histogram',
    createSeriesMarkers: () => ({ setMarkers() {} }),
    createChart: () => {
      const id = ++calls.created
      return {
        subscribeClick() {},
        applyOptions() {},
        addSeries: (_: unknown, opts: Record<string, unknown>) => series(opts),
        removeSeries() {},
        panes: () => [],
        timeScale: () => ({ fitContent: () => calls.fits.push(id) }),
        takeScreenshot: () => null,
        remove: () => void calls.removed++,
      }
    },
  }
})

const episode = (rounds: number): RunState =>
  applyEvents(emptyRunState, makeEpisode({ runId: 'run_PC', kind: 'exploratory', seed: 1, rounds, start: '2026-10-23T09:00:00Z', conditionId: null, branch: null }))

afterEach(() => {
  cleanup()
  Object.assign(calls, { created: 0, removed: 0, fits: [], lines: [] })
})

describe('price chart', () => {
  it('removes its chart on unmount, and a new chart for the same data fits the view again', () => {
    const state = episode(12)
    const first = render(<PriceChart state={state} overlays={[]} />)
    expect(calls.fits).toEqual([1])
    first.unmount()
    expect(calls.removed).toBe(1)
    render(<PriceChart state={state} overlays={[]} />)
    expect(calls.fits).toEqual([1, 2])
  })

  it('under StrictMode, the chart that stays on screen is fitted', () => {
    render(
      <StrictMode>
        <PriceChart state={episode(12)} overlays={[]} />
      </StrictMode>,
    )
    expect(calls.created).toBe(2)
    expect(calls.removed).toBe(1)
    expect(calls.fits).toContain(2)
  })

  it('draws sellers 5 and 6 dashed, because they repeat the colours of sellers 1 and 2', () => {
    const state = { ...episode(3), sellers: ['A', 'B', 'C', 'D', 'E', 'F'] }
    render(<PriceChart state={state} overlays={[]} />)
    const style = (id: string) => calls.lines.find((o) => o.title === id)?.lineStyle
    expect(['A', 'B', 'C', 'D'].map(style)).toEqual([0, 0, 0, 0])
    expect(['E', 'F'].map(style)).toEqual([2, 2])
  })

  it('names the sellers, the rounds, the overlays and the indicators in its label', () => {
    const state = episode(12)
    render(
      <PriceChart
        state={state}
        overlays={[{ id: 'run_B1', label: 'probe#0', state }]}
        indicators={[{ key: 'k', label: 'Δp', pane: 'separate', outputs: [] }]}
      />,
    )
    expect(screen.getByRole('img')).toHaveAccessibleName('Price chart for A, B, rounds R1 to R12; 1 branch overlay; indicators: Δp')
  })
})
