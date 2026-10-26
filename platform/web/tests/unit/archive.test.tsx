import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { http, HttpResponse } from 'msw/http'
import { setupServer } from 'msw/node'
import { DEFAULT_BRANCH_PLAN, type Experiment, type ExperimentCreate } from '@/api/experiments'
import { RunsPage } from '@/features/runs/RunsPage'
import { ExperimentsPage } from '@/features/experiments/ExperimentsPage'
import { handlers } from '@/mocks/handlers'
import { archivedRuns } from '@/mocks/archiveState'
import { mockRuns, resetMockRuns, summary } from '@/mocks/data'
import { blockMockCell, resetMockExperiments } from '@/mocks/experiments'

const requests: { method: string; path: string; body: string }[] = []
const server = setupServer(...handlers)
server.events.on('request:start', async ({ request }) => {
  const u = new URL(request.url)
  if (u.pathname.includes('archive')) requests.push({ method: request.method, path: u.pathname, body: await request.clone().text() })
})
beforeAll(() => server.listen({ onUnhandledFrame: 'bypass' }))
afterAll(() => server.close())
afterEach(() => {
  cleanup()
  server.resetHandlers()
  requests.length = 0
  localStorage.clear()
  resetMockRuns()
  resetMockExperiments()
})

function renderAt(path: string, element: React.ReactNode, route: string) {
  const router = createMemoryRouter([{ path: route, element }, { path: '/workbench/:id', element: <p>workbench</p> }, { path: '/experiments/:id', element: <p>experiment</p> }], { initialEntries: [path] })
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return router
}
const renderRuns = () => renderAt('/runs', <RunsPage />, '/runs')

const openActions = (id: string) => fireEvent.pointerDown(screen.getAllByRole('button', { name: `Actions for ${id}` })[0]!, { button: 0, ctrlKey: false })
// The Lineage text of a branch run links its parent, so a link or a text with the id is not enough: the compare checkbox is one per row.
const boxOf = (id: string) => screen.queryAllByRole('checkbox', { name: `Select ${id} to compare` })
const rowOf = (id: string) => boxOf(id)[0]!.closest('tr')!

/** The mock run_MOCK02 streams on a timer and may already have ended. These tests pin it live, so the answer does not depend on the clock. */
function pinLive() {
  const view = (r: (typeof mockRuns)[number]) => ({ ...summary(r), status: r.id === 'run_MOCK02' ? ('running' as const) : summary(r).status })
  server.use(
    http.get('/api/runs', ({ request }) => {
      const all = new URL(request.url).searchParams.get('include_archived') === 'true'
      return HttpResponse.json({ items: mockRuns.map(view).filter((s) => all || !s.archived_at), next_cursor: null })
    }),
    http.post('/api/runs/archive', async ({ request }) => {
      const b = (await request.json()) as { ids?: string[]; all?: true }
      if (!b.all) return HttpResponse.json({ title: 'x', status: 409, code: 'conflict', message: `Run ${b.ids![0]} is running. Stop it before you archive it.` }, { status: 409, headers: { 'Content-Type': 'application/problem+json' } })
      return HttpResponse.json({ count: mockRuns.length - 1, skipped: ['run_MOCK02'] })
    }),
  )
}

describe('runs: archive and restore (DECISIONS-29)', () => {
  it('Archive from the row menu sends the id, drops the run from the list and says so; Show archived brings it back marked, with Restore', async () => {
    renderRuns()
    await screen.findAllByText('run_MOCK01')
    openActions('run_MOCK01')
    fireEvent.click(await screen.findByRole('menuitem', { name: /Archive \(server, for everyone\)/ }))
    expect(await screen.findByRole('status')).toHaveTextContent('Archived 1 run.')
    expect(requests.at(-1)).toMatchObject({ method: 'POST', path: '/api/runs/archive' })
    expect(JSON.parse(requests.at(-1)!.body)).toEqual({ ids: ['run_MOCK01'] })
    await waitFor(() => expect(boxOf('run_MOCK01')).toHaveLength(0))
    expect(localStorage.getItem('runs.hidden.v1')).toBeNull()

    fireEvent.click(screen.getByRole('checkbox', { name: /Show archived runs/ }))
    await waitFor(() => expect(boxOf('run_MOCK01').length).toBeGreaterThan(0))
    expect(within(rowOf('run_MOCK01')).getByText('archived')).toBeInTheDocument()
    openActions('run_MOCK01')
    fireEvent.click(await screen.findByRole('menuitem', { name: /Restore \(server\)/ }))
    expect(await screen.findByText('Restored 1 run.')).toBeInTheDocument()
    await waitFor(() => expect(within(rowOf('run_MOCK01')).queryByText('archived')).toBeNull())
  })

  it('the wording keeps Hide (this browser) apart from Archive (the server, everyone)', async () => {
    renderRuns()
    await screen.findAllByText('run_MOCK01')
    expect(screen.getByText(/Hide changes only this list, in this browser\. Archive changes the server list for everyone and deletes nothing\./)).toBeInTheDocument()
    openActions('run_MOCK01')
    expect(await screen.findByRole('menuitem', { name: /Hide from the list \(this browser\)/ })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: /Archive \(server, for everyone\)/ })).toBeInTheDocument()
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
    expect(screen.getByRole('checkbox', { name: /Show hidden runs/ })).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: /Show archived runs \(server\)/ })).toBeInTheDocument()
  })

  it('a running run: the server says 409 and the page shows its message, with the run still listed', async () => {
    pinLive()
    renderRuns()
    await screen.findAllByText('run_MOCK02')
    openActions('run_MOCK02')
    fireEvent.click(await screen.findByRole('menuitem', { name: /Archive \(server, for everyone\)/ }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Run run_MOCK02 is running. Stop it before you archive it.')
    expect(screen.getAllByText('run_MOCK02').length).toBeGreaterThan(0)
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('Archive all asks first with the server count and the shown count, and names the running runs; the confirm sends all and names the skipped runs', async () => {
    pinLive()
    renderRuns()
    await screen.findAllByText('run_MOCK01')
    fireEvent.click(screen.getByRole('button', { name: 'Archive all' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByRole('heading', { name: `Archive all ${mockRuns.length} runs on the server (not only the ${mockRuns.length} shown)?` })).toBeInTheDocument()
    const todo = `Archive ${mockRuns.length - 1} of ${mockRuns.length} runs (1 is running)`
    await waitFor(() => expect(within(dialog).getByRole('button', { name: todo })).toBeEnabled())
    expect(dialog).toHaveTextContent(`This archives ${mockRuns.length - 1} of ${mockRuns.length} runs on the server, for everyone.`)
    expect(dialog).toHaveTextContent('Nothing is deleted')
    expect(dialog).toHaveTextContent('This is not Hide, which changes only this browser.')
    expect(dialog).toHaveTextContent('1 run is running. It stays in the list')
    expect(requests).toEqual([])
    fireEvent.click(within(dialog).getByRole('button', { name: todo }))
    expect(await screen.findByRole('status')).toHaveTextContent(`Archived ${mockRuns.length - 1} runs. Skipped 1 run that is running: run_MOCK02.`)
    expect(JSON.parse(requests.at(-1)!.body)).toEqual({ all: true })
  })

  it('Cancel in the Archive all dialog sends nothing', async () => {
    renderRuns()
    await screen.findAllByText('run_MOCK01')
    fireEvent.click(screen.getByRole('button', { name: 'Archive all' }))
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(requests).toEqual([])
  })

  it('with a filter on, the title gives the server count N and the shown count M, and the request still says all', async () => {
    const research = mockRuns.filter((r) => r.header.kind === 'research').length
    expect(research).toBeLessThan(mockRuns.length)
    renderAt('/runs?kind=research', <RunsPage />, '/runs')
    await waitFor(() => expect(boxOf('run_MOCK01').length).toBeGreaterThan(0))
    expect(boxOf('run_MOCK02')).toHaveLength(0)
    fireEvent.click(screen.getByRole('button', { name: 'Archive all' }))
    const dialog = await screen.findByRole('dialog')
    await waitFor(() => expect(within(dialog).getByRole('heading')).toHaveTextContent(`Archive all ${mockRuns.length} runs on the server (not only the ${research} shown)?`))
    fireEvent.click(within(dialog).getByRole('button', { name: /^Archive \d+( of \d+)? runs/ }))
    await screen.findByRole('status')
    expect(JSON.parse(requests.at(-1)!.body)).toEqual({ all: true })
    expect(archivedRuns.size).toBeGreaterThanOrEqual(research)
  })

  it('a queued run is not running: it archives from its row', async () => {
    server.use(http.get('/api/runs', () => HttpResponse.json({ items: mockRuns.map((r) => ({ ...summary(r), status: r.id === 'run_MOCK03' ? ('queued' as const) : summary(r).status })), next_cursor: null })))
    renderRuns()
    await screen.findAllByText('run_MOCK03')
    openActions('run_MOCK03')
    fireEvent.click(await screen.findByRole('menuitem', { name: /Archive \(server, for everyone\)/ }))
    expect(await screen.findByRole('status')).toHaveTextContent('Archived 1 run.')
  })

  it('Restore all shows only with the archived runs on, asks first, and restores them', async () => {
    archivedRuns.set('run_MOCK01', '2026-10-23T15:00:00Z')
    archivedRuns.set('run_MOCK03', '2026-10-23T15:00:00Z')
    renderRuns()
    await screen.findAllByText('run_MOCK02')
    expect(screen.queryByRole('button', { name: 'Restore all' })).toBeNull()
    fireEvent.click(screen.getByRole('checkbox', { name: /Show archived runs/ }))
    fireEvent.click(await screen.findByRole('button', { name: 'Restore all' }))
    const dialog = await screen.findByRole('dialog')
    await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Restore 2 runs' })).toBeEnabled())
    expect(within(dialog).getByRole('heading')).toHaveTextContent('Restore all 2 runs that are archived on the server (not only the 2 shown)?')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Restore 2 runs' }))
    expect(await screen.findByText('Restored 2 runs.')).toBeInTheDocument()
    expect(JSON.parse(requests.at(-1)!.body)).toEqual({ all: true })
    expect(requests.at(-1)!.path).toBe('/api/runs/unarchive')
    expect(archivedRuns.size).toBe(0)
  })

  it('the selection restores its archived runs and archives the others', async () => {
    archivedRuns.set('run_MOCK01', '2026-10-23T15:00:00Z')
    renderRuns()
    await screen.findAllByText('run_MOCK03')
    fireEvent.click(screen.getByRole('checkbox', { name: /Show archived runs/ }))
    await screen.findAllByText('run_MOCK01')
    fireEvent.click(screen.getAllByRole('checkbox', { name: 'Select run_MOCK01 to compare' })[0]!)
    fireEvent.click(screen.getAllByRole('checkbox', { name: 'Select run_MOCK03 to compare' })[0]!)
    expect(screen.getByRole('button', { name: 'Restore 1 selected' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Archive 1 selected' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Restore 1 selected' }))
    expect(await screen.findByText('Restored 1 run.')).toBeInTheDocument()
    expect(JSON.parse(requests.at(-1)!.body)).toEqual({ ids: ['run_MOCK01'] })
    // A success clears the selection, so the bar goes.
    await waitFor(() => expect(screen.queryByRole('button', { name: /selected/ })).toBeNull())
    expect(screen.queryByRole('button', { name: 'Clear selection' })).toBeNull()
  })

  it('a refused selection keeps the selection', async () => {
    pinLive()
    renderRuns()
    await screen.findAllByText('run_MOCK02')
    fireEvent.click(screen.getAllByRole('checkbox', { name: 'Select run_MOCK02 to compare' })[0]!)
    fireEvent.click(screen.getByRole('button', { name: 'Archive 1 selected' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('is running')
    expect(screen.getByRole('button', { name: 'Archive 1 selected' })).toBeInTheDocument()
  })
})

const body = (name: string): ExperimentCreate => ({
  conditions: ['C1'],
  split: 'pilot',
  seeds: [1],
  agents: ['A', 'B'].map((id) => ({ id, role: 'seller', policy: { kind: 'constant', params: { price: 5 } }, capability: null })) as never,
  budget: { max_usd: 0.5 },
  batch_budget: { max_usd: 5 },
  branch_plan: structuredClone(DEFAULT_BRANCH_PLAN),
  name,
})
/** A new mock experiment is running (its cells are live). `idle` blocks its one cell, so the status is blocked and the experiment can archive. */
const make = async (name: string, idle = true) => {
  const r = await fetch(`${location.origin}/api/experiments`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body(name)) })
  const id = ((await r.json()) as Experiment).id
  if (idle) blockMockCell(id, 'C1', 1, 'hash_mismatch', 'Scenario changed.', { stuck: true })
  return id
}

describe('experiments: archive and restore (DECISIONS-29)', () => {
  it('Archive on a row sends the id; the experiment leaves the list; Show archived lists it marked, with Restore', async () => {
    const id = await make('first')
    await make('second')
    renderAt('/experiments', <ExperimentsPage />, '/experiments')
    await screen.findByText('first')
    const row = () => screen.getByText('first').closest('tr')!
    fireEvent.click(within(row()).getByRole('button', { name: 'Archive' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Archived 1 experiment.')
    expect(JSON.parse(requests.at(-1)!.body)).toEqual({ ids: [id] })
    await waitFor(() => expect(screen.queryByText('first')).toBeNull())
    expect(screen.getByText('second')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('checkbox', { name: /Show archived experiments/ }))
    await screen.findByText('first')
    expect(within(row()).getByText('archived')).toBeInTheDocument()
    fireEvent.click(within(row()).getByRole('button', { name: 'Restore' }))
    expect(await screen.findByText('Restored 1 experiment.')).toBeInTheDocument()
    await waitFor(() => expect(within(row()).queryByText('archived')).toBeNull())
  })

  it('a running experiment: its row button is off with the reason; by selection the server says 409; with all it is skipped and named', async () => {
    const live = await make('live', false)
    await make('idle')
    renderAt('/experiments', <ExperimentsPage />, '/experiments')
    await screen.findByText('live')
    const row = (name: string) => screen.getByText(name).closest('tr')!
    const off = within(row('live')).getByRole('button', { name: 'Archive' })
    expect(off).toBeDisabled()
    expect(off).toHaveAttribute('title', 'A running experiment cannot be archived. Stop it or wait until it ends.')
    expect(within(row('idle')).getByRole('button', { name: 'Archive' })).toBeEnabled()
    fireEvent.click(within(row('live')).getByRole('checkbox'))
    fireEvent.click(screen.getByRole('button', { name: 'Archive 1 selected' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(`Experiment ${live} is running. Stop it before you archive it.`)
    fireEvent.click(screen.getByRole('button', { name: 'Archive all' }))
    const dialog = await screen.findByRole('dialog')
    await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Archive 1 of 2 experiments (1 is running)' })).toBeEnabled())
    expect(dialog).toHaveTextContent('This archives 1 of 2 experiments on the server, for everyone.')
    expect(dialog).toHaveTextContent('1 experiment is running. It stays in the list')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Archive 1 of 2 experiments (1 is running)' }))
    expect(await screen.findByRole('status')).toHaveTextContent(`Archived 1 experiment. Skipped 1 experiment that is running: ${live}.`)
  })

  it('Archive all asks with the count, then archives; the selection and Restore all bring them back', async () => {
    await make('first')
    await make('second')
    await make('third')
    renderAt('/experiments', <ExperimentsPage />, '/experiments')
    await screen.findByText('third')
    fireEvent.click(screen.getByRole('button', { name: 'Archive all' }))
    const dialog = await screen.findByRole('dialog')
    await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Archive 3 experiments' })).toBeEnabled())
    expect(within(dialog).getByRole('heading')).toHaveTextContent('Archive all 3 experiments on the server (not only the 3 shown)?')
    expect(dialog).toHaveTextContent('This archives 3 experiments on the server, for everyone.')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Archive 3 experiments' }))
    expect(await screen.findByText('Archived 3 experiments.')).toBeInTheDocument()
    expect(JSON.parse(requests.at(-1)!.body)).toEqual({ all: true })
    expect(await screen.findByText('No experiments yet.')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('checkbox', { name: /Show archived experiments/ }))
    await screen.findByText('first')
    const boxes = screen.getAllByRole('checkbox', { name: /to archive or restore/ })
    fireEvent.click(boxes[0]!)
    fireEvent.click(boxes[1]!)
    fireEvent.click(screen.getByRole('button', { name: 'Restore 2 selected' }))
    expect(await screen.findByText('Restored 2 experiments.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Restore all' }))
    const again = await screen.findByRole('dialog')
    await waitFor(() => expect(within(again).getByRole('button', { name: 'Restore 1 experiment' })).toBeEnabled())
    expect(within(again).getByRole('heading')).toHaveTextContent('Restore all 1 experiment that is archived on the server (not only the 1 shown)?')
  })
})
