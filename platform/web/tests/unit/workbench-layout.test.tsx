import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryRouter, Navigate, RouterProvider } from 'react-router'
import { http, HttpResponse } from 'msw/http'
import { setupServer } from 'msw/node'
import { handlers, wsHandlers } from '@/mocks/handlers'
import { findRun } from '@/mocks/data'
import { WorkbenchLayout } from '@/features/workbench/WorkbenchLayout'

// jsdom has no canvas: the charts only need an object that accepts calls.
vi.mock('lightweight-charts', () => {
  const any: unknown = new Proxy(() => any, { get: (_, k) => (k === 'then' ? undefined : any), apply: () => any })
  return { ColorType: {}, LineStyle: {}, LineType: {}, LineSeries: 'Line', HistogramSeries: 'Histogram', createSeriesMarkers: () => any, createChart: () => any }
})

const gets: string[] = []
const server = setupServer(...handlers, ...wsHandlers)
server.events.on('request:start', ({ request }) => void (request.method === 'GET' && gets.push(new URL(request.url).pathname)))
beforeAll(() => {
  server.listen({ onUnhandledFrame: 'bypass' })
})
afterAll(() => server.close())
afterEach(() => {
  cleanup()
  server.resetHandlers()
  vi.restoreAllMocks()
  vi.useRealTimers()
  sessionStorage.clear()
  gets.length = 0
})

const EXP = {
  id: 'exp_TREE1',
  name: 'Tree test',
  hash: 'sha256:x',
  created_at: '2026-10-22T10:00:00Z',
  status: 'running',
  split: 'pilot',
  conditions: ['C1'],
  seeds: [101],
  counts: { running: 1 },
  spend: { calls: 0, usd: 0, tokens: 0, unpriced: 0 },
}
const CELLS = [
  {
    condition_id: 'C1',
    seed: 101,
    status: 'branching',
    error: null,
    parent: { run_id: 'run_MOCK01', status: 'completed', end_reason: 'horizon' },
    branches: [
      { run_id: 'run_MOCK01PROBE', status: 'completed', end_reason: 'horizon', kind: 'probe', index: 0 },
      { run_id: 'run_PLANNED9', status: 'planned', end_reason: null, kind: 'ablation', index: 20 },
    ],
  },
]
const experiments = () => [
  http.get('/api/experiments', () => HttpResponse.json([EXP])),
  http.get('/api/experiments/:id', () => HttpResponse.json({ ...EXP, branch_plan: { probe: { at: 20, rounds: 5, target: 'B' }, placebo: null, ablation: { at: 10, mode: 'shuffle' } }, estimate: {}, cells: CELLS })),
]

function renderAt(path: string) {
  const router = createMemoryRouter(
    [
      {
        path: '/workbench',
        children: [
          { index: true, element: <Navigate to="new" replace /> },
          { element: <WorkbenchLayout />, children: [{ path: 'new', element: null }, { path: ':runId', element: null }] },
        ],
      },
    ],
    { initialEntries: [path] },
  )
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return router
}

const tabs = () => screen.getByRole('list', { name: 'Open runs' })
const rail = () => screen.getByRole('navigation', { name: 'Runs and experiments' })

describe('workbench rail and tabs (UI-WORKBENCH.md §6 step 4)', () => {
  it('/workbench opens setup; a run in the rail opens a tab with its id, condition and status', async () => {
    const router = renderAt('/workbench')
    await waitFor(() => expect(router.state.location.pathname).toBe('/workbench/new'))
    expect(await screen.findByRole('button', { name: 'Add seller' })).toBeInTheDocument()
    fireEvent.click(await within(rail()).findByRole('link', { name: /run_MOCK03.*C3/ }))
    expect(router.state.location.pathname).toBe('/workbench/run_MOCK03')
    const tab = await within(tabs()).findByRole('link', { name: /run_MOCK03/ })
    await waitFor(() => expect(tab).toHaveTextContent(/C3/))
    expect(tab).toHaveTextContent(/completed|stopped|running/)
  })

  it('a second run adds a second tab; closing the active tab opens its neighbour, closing the last opens setup', async () => {
    const router = renderAt('/workbench/run_MOCK01')
    await within(tabs()).findByRole('link', { name: /run_MOCK01/ })
    fireEvent.click(await within(rail()).findByRole('link', { name: /run_MOCK03/ }))
    await waitFor(() => expect(within(tabs()).getAllByRole('button', { name: /^Close tab/ })).toHaveLength(2))
    fireEvent.click(within(tabs()).getByRole('button', { name: 'Close tab run_MOCK03' }))
    await waitFor(() => expect(router.state.location.pathname).toBe('/workbench/run_MOCK01'))
    fireEvent.click(within(tabs()).getByRole('button', { name: 'Close tab run_MOCK01' }))
    await waitFor(() => expect(router.state.location.pathname).toBe('/workbench/new'))
    expect(within(tabs()).queryAllByRole('button', { name: /^Close tab/ })).toHaveLength(0)
  })

  it('closing a tab that is not active keeps the page', async () => {
    sessionStorage.setItem('workbench-tabs', JSON.stringify(['run_MOCK01', 'run_MOCK03']))
    const router = renderAt('/workbench/new')
    await screen.findByRole('button', { name: 'Add seller' })
    fireEvent.click(within(tabs()).getByRole('button', { name: 'Close tab run_MOCK01' }))
    await waitFor(() => expect(within(tabs()).getAllByRole('button', { name: /^Close tab/ })).toHaveLength(1))
    expect(router.state.location.pathname).toBe('/workbench/new')
  })

  it('the open tabs come back after a remount (session storage)', async () => {
    renderAt('/workbench/run_MOCK01')
    await within(tabs()).findByRole('link', { name: /run_MOCK01/ })
    cleanup()
    renderAt('/workbench/new')
    expect(await within(tabs()).findByRole('link', { name: /run_MOCK01/ })).toBeInTheDocument()
  })

  it('the setup draft stays when the user opens a run tab and comes back', async () => {
    renderAt('/workbench/new')
    await screen.findByRole('button', { name: 'Add seller' })
    fireEvent.change(document.getElementById('f-budget.max_calls')!, { target: { value: '17' } })
    fireEvent.click(await within(rail()).findByRole('link', { name: /run_MOCK03/ }))
    await within(tabs()).findByRole('button', { name: 'Close tab run_MOCK03' })
    fireEvent.click(within(tabs()).getByRole('link', { name: 'New run (setup)' }))
    await waitFor(() => expect(document.getElementById('f-budget.max_calls')).toHaveValue(17))
  })

  it('an experiment opens to its cells and branch runs; a planned run is not a link', async () => {
    server.use(...experiments())
    const router = renderAt('/workbench/new')
    const node = await within(rail()).findByText('Tree test')
    fireEvent.click(node)
    const cell = await within(rail()).findByRole('link', { name: /C1 · seed 101/ })
    // The label names the fork round and the change, not the plan index (A7 finding 7).
    expect(within(rail()).getByRole('link', { name: /probe B at R20/ })).toHaveAttribute('href', '/workbench/run_MOCK01PROBE')
    expect(within(rail()).getByText('ablation (shuffle) from R10').closest('a')).toBeNull()
    expect(within(rail()).queryByText(/probe 0|ablation 20/)).toBeNull()
    fireEvent.click(cell)
    expect(router.state.location.pathname).toBe('/workbench/run_MOCK01')
  })

  it('labels a run with its experiment (experiment_id); a branch child without one gets its parent\'s (step 5)', async () => {
    server.use(...experiments())
    const main = findRun('run_MOCK01')!
    main.experimentId = 'exp_TREE1'
    try {
      renderAt('/workbench/run_MOCK01PROBE')
      expect(await within(rail()).findByRole('link', { name: /run_MOCK01 · C1 · in Tree test/ })).toBeInTheDocument()
      expect(within(rail()).getByRole('link', { name: /run_MOCK03 · C3/ })).not.toHaveTextContent(/ in /)
      const tab = await within(tabs()).findByRole('link', { name: /01PROBE/ })
      await waitFor(() => expect(tab).toHaveAttribute('title', 'run_MOCK01PROBE (branch of a run in Tree test)'))
    } finally {
      main.experimentId = undefined
    }
  })

  // CR-52
  it('works for the visit when session storage throws', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    const router = renderAt('/workbench/run_MOCK01')
    await within(tabs()).findByRole('button', { name: 'Close tab run_MOCK01' })
    fireEvent.click(await within(rail()).findByRole('link', { name: /run_MOCK03/ }))
    await waitFor(() => expect(within(tabs()).getAllByRole('button', { name: /^Close tab/ })).toHaveLength(2))
    expect(router.state.location.pathname).toBe('/workbench/run_MOCK03')
  })

  it('a missing run says not found, gets no tab, stops polling, and Close opens setup', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const router = renderAt('/workbench/run_NOPE')
    expect(await screen.findByText('Run run_NOPE was not found.')).toBeInTheDocument()
    expect(within(tabs()).queryByRole('button', { name: 'Close tab run_NOPE' })).toBeNull()
    expect(sessionStorage.getItem('workbench-tabs') ?? '[]').not.toContain('run_NOPE')
    const asked = gets.filter((p) => p === '/api/runs/run_NOPE').length
    await vi.advanceTimersByTimeAsync(10_000)
    expect(gets.filter((p) => p === '/api/runs/run_NOPE').length).toBe(asked)
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(router.state.location.pathname).toBe('/workbench/new'))
  })

  it('a stored tab whose run is gone leaves the bar', async () => {
    sessionStorage.setItem('workbench-tabs', JSON.stringify(['run_GONE', 'run_MOCK01']))
    renderAt('/workbench/new')
    await within(tabs()).findByRole('button', { name: 'Close tab run_MOCK01' })
    await waitFor(() => expect(within(tabs()).queryByRole('button', { name: 'Close tab run_GONE' })).toBeNull())
    expect(JSON.parse(sessionStorage.getItem('workbench-tabs')!)).toEqual(['run_MOCK01'])
  })

  it('after a start, the setup draft stays but Start needs a new estimate', async () => {
    const router = renderAt('/workbench/new')
    await screen.findByRole('button', { name: 'Add seller' })
    for (const i of [0, 1]) fireEvent.change(document.getElementById(`f-agents.${i}.policy.kind`)!, { target: { value: 'constant' } })
    fireEvent.change(document.getElementById('f-budget.max_calls')!, { target: { value: '10' } })
    fireEvent.click(screen.getByRole('button', { name: /Estimate cost/ }))
    await screen.findByText(/Maximum cost/)
    fireEvent.click(screen.getByRole('button', { name: 'Start 1 run' }))
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Start' }))
    await waitFor(() => expect(router.state.location.pathname).toMatch(/^\/workbench\/run_MOCKNEW\d+_0$/))
    fireEvent.click(within(tabs()).getByRole('link', { name: 'New run (setup)' }))
    await waitFor(() => expect(router.state.location.pathname).toBe('/workbench/new'))
    expect(document.getElementById('f-budget.max_calls')).toHaveValue(10)
    expect(screen.getByRole('button', { name: 'Start 1 run' })).toBeDisabled()
    expect(screen.queryByText(/Maximum cost/)).toBeNull()
  })

})
