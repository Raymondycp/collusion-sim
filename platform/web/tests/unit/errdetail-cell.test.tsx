import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { http, HttpResponse } from 'msw/http'
import { setupServer } from 'msw/node'
import { DEFAULT_BRANCH_PLAN, type Experiment, type ExperimentCreate } from '@/api/experiments'
import { ExperimentPage } from '@/features/experiments/ExperimentsPage'
import { handlers } from '@/mocks/handlers'
import { mockRuns, summary } from '@/mocks/data'
import { resetMockExperiments } from '@/mocks/experiments'

const lookups: string[] = []
const server = setupServer(...handlers)
server.events.on('request:start', ({ request }) => {
  const p = new URL(request.url).pathname
  if (/^\/api\/runs\/[^/]+$/.test(p)) lookups.push(p)
})
beforeAll(() => server.listen({ onUnhandledFrame: 'bypass' }))
afterAll(() => server.close())
afterEach(() => {
  cleanup()
  server.resetHandlers()
  resetMockExperiments()
  lookups.length = 0
})

const detail = { code: 'auth', exception: 'EndpointError', http_status: 401, provider_code: 'invalid_api_key', provider_message: 'Incorrect key [redacted]', message: 'EndpointError: 401', where: { phase: 'act', seller_id: 'A', t: 5 } }

const request: ExperimentCreate = {
  conditions: ['C1'],
  split: 'pilot',
  seeds: [1],
  agents: ['A', 'B'].map((id) => ({ id, role: 'seller', policy: { kind: 'constant', params: { price: 5 } }, capability: null })) as never,
  budget: { max_usd: 0.5 },
  batch_budget: { max_usd: 5 },
  branch_plan: structuredClone(DEFAULT_BRANCH_PLAN),
}

/** An experiment whose only cell has a parent run in `parentStatus`. The parent run answers with `error_detail` (or none). */
async function renderExperiment(parentStatus: 'failed' | 'completed', error_detail: unknown) {
  const made = (await (await fetch(`${location.origin}/api/experiments`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(request) })).json()) as Experiment
  const parentId = made.cells[0]!.parent.run_id
  server.use(
    http.get(`/api/experiments/${made.id}`, () => HttpResponse.json({ ...made, cells: made.cells.map((c, i) => (i === 0 ? { ...c, parent: { ...c.parent, status: parentStatus, end_reason: parentStatus === 'failed' ? 'error' : 'horizon' } } : c)) })),
    http.get(`/api/runs/${parentId}`, () => HttpResponse.json({ ...summary(mockRuns[2]!), id: parentId, status: parentStatus, config: {}, scenario_hash: 'h', error: error_detail ? 'auth' : null, error_detail })),
  )
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RouterProvider router={createMemoryRouter([{ path: '/experiments/:experimentId', element: <ExperimentPage /> }], { initialEntries: [`/experiments/${made.id}`] })} />
    </QueryClientProvider>,
  )
  return parentId
}

describe('error detail in the experiment cell (CR-104 gap 2)', () => {
  it('a failed parent shows the kind, HTTP status, provider code and message, and the place, from one lookup of that run', async () => {
    const parentId = await renderExperiment('failed', detail)
    const cell = (await screen.findByRole('table', { name: 'Cells' })).querySelector('[data-cell="C1/1"]') as HTMLElement
    const list = await within(cell).findByRole('list', { name: 'Error detail' })
    expect(within(list).getAllByRole('listitem').map((x) => x.textContent)).toEqual([
      'auth (EndpointError) HTTP 401 provider code invalid_api_key',
      'Provider message: Incorrect key [redacted]',
      'During act of seller A in round 5.',
    ])
    expect(cell).toHaveTextContent('failed, error')
    await new Promise((r) => setTimeout(r, 100))
    expect(lookups.filter((p) => p === `/api/runs/${parentId}`)).toHaveLength(1)
  })

  it('a parent that did not fail causes no lookup and shows no list', async () => {
    await renderExperiment('completed', null)
    const cell = (await screen.findByRole('table', { name: 'Cells' })).querySelector('[data-cell="C1/1"]') as HTMLElement
    await waitFor(() => expect(cell).toHaveTextContent('completed'))
    await new Promise((r) => setTimeout(r, 100))
    expect(within(cell).queryByRole('list', { name: 'Error detail' })).toBeNull()
    expect(lookups).toEqual([])
  })

  it('a failed parent without a detail (an older run) keeps the status line and shows no list', async () => {
    const parentId = await renderExperiment('failed', null)
    const cell = (await screen.findByRole('table', { name: 'Cells' })).querySelector('[data-cell="C1/1"]') as HTMLElement
    await waitFor(() => expect(lookups).toContain(`/api/runs/${parentId}`))
    await new Promise((r) => setTimeout(r, 100))
    expect(cell).toHaveTextContent('failed, error')
    expect(within(cell).queryByRole('list', { name: 'Error detail' })).toBeNull()
  })
})
