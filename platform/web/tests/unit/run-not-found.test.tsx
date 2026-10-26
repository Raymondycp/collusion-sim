import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { http, HttpResponse } from 'msw/http'
import { setupServer } from 'msw/node'
import { handlers } from '@/mocks/handlers'
import { WorkbenchPage } from '@/features/workbench/WorkbenchPage'

// jsdom has no canvas. The page only needs a chart object that accepts calls.
vi.mock('lightweight-charts', () => {
  const any: unknown = new Proxy(() => any, { get: (_, k) => (k === 'then' ? undefined : any), apply: () => any })
  return { ColorType: {}, LineStyle: {}, LineType: {}, LineSeries: 'Line', HistogramSeries: 'Histogram', createSeriesMarkers: () => any, createChart: () => any }
})
vi.mock('@/components/chart/PriceChart', async (orig) => ({ ...(await orig<typeof import('@/components/chart/PriceChart')>()), PriceChart: () => null }))
vi.mock('@/features/run-view/TimelineBar', () => ({ TimelineBar: () => null }))

// No WebSocket handler: the socket cannot connect, so only the REST answer can say "not found".
const server = setupServer(...handlers)
beforeAll(() => server.listen({ onUnhandledFrame: 'bypass' }))
afterAll(() => server.close())
afterEach(() => {
  cleanup()
  server.resetHandlers()
})

function renderRun(id: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const router = createMemoryRouter([{ path: '/workbench/:runId', element: <WorkbenchPage /> }], { initialEntries: [`/workbench/${id}`] })
  render(
    <QueryClientProvider client={qc}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

describe('a run that does not exist (workbench)', () => {
  it('says so and links to the run list, without a socket', async () => {
    server.use(
      http.get('/api/runs/run_MISSING', () =>
        HttpResponse.json(
          { title: 'No such run: run_MISSING', status: 404, code: 'not_found', message: 'No such run: run_MISSING' },
          { status: 404, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    renderRun('run_MISSING')
    expect(await screen.findByText('Run run_MISSING was not found.')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Go to the run list' })).toHaveAttribute('href', '/runs')
  })
})
