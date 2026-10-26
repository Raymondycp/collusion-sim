import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { setupServer } from 'msw/node'
import { RunsPage } from '@/features/runs/RunsPage'
import { CloneDialog } from '@/features/runs/CloneDialog'
import { overridable, parseValue, toOverrides, toPointer } from '@/features/runs/clone'
import { handlers } from '@/mocks/handlers'
import { resetMockRuns } from '@/mocks/data'

describe('clone overrides', () => {
  it('turns form rows into JSON-pointer overrides typed like the current value', () => {
    const fields = new Map<string, unknown>([
      ['agents.0.policy.llm.temperature', 0.7],
      ['channels.public', true],
      ['agents.0.policy.llm.system_prompt', 'Sell fuel.'],
      ['indicators', null],
    ])
    expect(
      toOverrides(
        [
          { path: 'agents.0.policy.llm.temperature', text: '0.2' },
          { path: 'channels.public', text: 'false' },
          { path: 'agents.0.policy.llm.system_prompt', text: ' Sell fuel, carefully. ' },
          { path: 'indicators', text: '["delta_p"]' },
        ],
        fields,
      ),
    ).toEqual({
      overrides: {
        '/agents/0/policy/llm/temperature': 0.2,
        '/channels/public': false,
        '/agents/0/policy/llm/system_prompt': ' Sell fuel, carefully. ',
        '/indicators': ['delta_p'],
      },
    })
  })

  it('reports each bad row and sends no override', () => {
    const fields = new Map<string, unknown>([['budget.max_usd', 0.5], ['channels.news', true]])
    expect(
      toOverrides(
        [
          { path: 'budget.max_usd', text: 'cheap' },
          { path: 'channels.news', text: 'yes' },
          { path: '', text: '1' },
          { path: 'budget.max_usd', text: '1' },
        ],
        fields,
      ),
    ).toEqual({ errors: { 0: 'Enter a number.', 1: 'Enter true or false.', 2: 'Select a field.' } })
    expect(parseValue('{', null)).toEqual({ error: 'Enter JSON, for example null, 3 or "text".' })
    expect(toPointer('a/b.c~d')).toBe('/a~1b/c~0d')
  })

  it('offers settings only, not ids or the scenario hash', () => {
    const keys = overridable({ name: 'x', scenario_hash: 'h', preset_id: 'p', agents: [{ id: 'A', policy: { kind: 'llm' } }], seeds: [7] }).map(([k]) => k)
    expect(keys).toEqual(['agents.0.policy.kind', 'seeds.0'])
  })
})

const requests: { method: string; url: URL; body: string }[] = []
const server = setupServer(...handlers)
server.events.on('request:start', async ({ request }) => {
  requests.push({ method: request.method, url: new URL(request.url), body: await request.clone().text() })
})
beforeAll(() => server.listen({ onUnhandledFrame: 'bypass' }))
afterAll(() => server.close())
afterEach(() => {
  cleanup()
  requests.length = 0
  resetMockRuns()
})

function renderAt(path: string, element: React.ReactNode) {
  const router = createMemoryRouter(
    [
      { path: '/runs', element },
      { path: '/runs/compare', element: <p>compare page</p> },
      { path: '/runs/:id', element: <p>run page</p> },
      { path: '/workbench/:id', element: <p>workbench</p> },
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

const openActions = (id: string) => {
  const trigger = screen.getAllByRole('button', { name: `Actions for ${id}` })[0]!
  fireEvent.pointerDown(trigger, { button: 0, ctrlKey: false })
}

describe('history list', () => {
  it('sends the URL filters to the ledger query', async () => {
    renderAt('/runs?model=mock-model&created_from=2026-10-23&created_to=2026-10-23&kind=research', <RunsPage />)
    await screen.findAllByText('run_MOCK01')
    const q = requests.find((r) => r.url.pathname === '/api/runs')!.url.searchParams
    expect(Object.fromEntries(q)).toMatchObject({ model: 'mock-model', created_from: '2026-10-23', created_to: '2026-10-23', kind: 'research' })
    expect(screen.queryAllByText('run_MOCK03')).toHaveLength(0)
  })

  it('hides a run in this browser only, and shows it again on request', async () => {
    renderAt('/runs', <RunsPage />)
    await screen.findAllByText('run_MOCK02')
    openActions('run_MOCK02')
    fireEvent.click(await screen.findByRole('menuitem', { name: /Hide from the list/ }))
    await waitFor(() => expect(screen.queryAllByText('run_MOCK02')).toHaveLength(0))
    expect(JSON.parse(localStorage.getItem('runs.hidden.v1')!)).toEqual(['run_MOCK02'])
    expect(requests.filter((r) => r.method !== 'GET')).toEqual([])

    expect(screen.getByLabelText('Result count')).toHaveTextContent('of 7 loaded (1 hidden).')
    fireEvent.click(screen.getByRole('checkbox', { name: 'Show hidden runs (1)' }))
    expect(screen.getByLabelText('Result count')).not.toHaveTextContent('hidden)')
    expect((await screen.findAllByText('run_MOCK02')).length).toBeGreaterThan(0)
    expect(screen.getAllByText('hidden').length).toBeGreaterThan(0)

    openActions('run_MOCK02')
    fireEvent.click(await screen.findByRole('menuitem', { name: /Show in the list/ }))
    await waitFor(() => expect(screen.queryAllByText('hidden')).toHaveLength(0))
    expect(JSON.parse(localStorage.getItem('runs.hidden.v1')!)).toEqual([])
  })

  it('opens a run by its exact ID', async () => {
    const router = renderAt('/runs', <RunsPage />)
    await screen.findAllByText('run_MOCK01')
    const open = screen.getByRole('button', { name: 'Open' })
    expect(open).toBeDisabled()
    fireEvent.change(screen.getByRole('textbox', { name: 'Run ID' }), { target: { value: ' run_MOCK02 ' } })
    fireEvent.click(open)
    await waitFor(() => expect(router.state.location.pathname).toBe('/runs/run_MOCK02'))
  })

  it('searches the loaded rows by name and says how many show, of how many loaded, and the 200-row limit', async () => {
    renderAt('/runs', <RunsPage />)
    await screen.findAllByText('run_MOCK01')
    const count = screen.getByLabelText('Result count')
    expect(count).toHaveTextContent(/of 7 loaded\./)
    expect(count).toHaveTextContent('at most the newest 200 runs')
    expect(count).toHaveTextContent('this list is complete')
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search by name' }), { target: { value: 'seed 7' } })
    await waitFor(() => expect(screen.queryAllByText('run_MOCK01')).toHaveLength(0))
    expect(screen.getAllByText('run_MOCK02').length).toBeGreaterThan(0)
    expect(screen.getByLabelText('Result count')).toHaveTextContent('2 runs shown (name contains "seed 7"), of 7 loaded.')
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search by name' }), { target: { value: 'no such name' } })
    expect(await screen.findByText('No loaded run has this name.')).toBeInTheDocument()
  })

  it('compares only when 2 or more runs are selected', async () => {
    const router = renderAt('/runs', <RunsPage />)
    await screen.findAllByText('run_MOCK01')
    fireEvent.click(screen.getAllByRole('checkbox', { name: 'Select run_MOCK01 to compare' })[0]!)
    expect(screen.getByRole('button', { name: /Compare \(select 2 or more\)/ })).toBeDisabled()
    fireEvent.click(screen.getAllByRole('checkbox', { name: 'Select run_MOCK02 to compare' })[0]!)
    fireEvent.click(screen.getByRole('link', { name: /Compare 2 runs/ }))
    await waitFor(() => expect(router.state.location.pathname).toBe('/runs/compare'))
    expect(router.state.location.search).toBe('?ids=run_MOCK01,run_MOCK02')
  })
})

describe('clone dialog', () => {
  it('exploratory: sends the seed and the changes, then opens the new run', async () => {
    const router = renderAt('/runs', <CloneDialog runId="run_MOCK02" onClose={() => {}} />)
    const dialog = await screen.findByRole('dialog')
    await within(dialog).findByText('Changes')
    fireEvent.click(within(dialog).getByRole('radio', { name: 'Another seed' }))
    fireEvent.change(within(dialog).getByLabelText('Seed'), { target: { value: '8' } })
    fireEvent.click(within(dialog).getByRole('button', { name: /Add a change/ }))
    fireEvent.change(within(dialog).getByLabelText('Field 1'), { target: { value: 'agents.0.policy.llm.temperature' } })
    expect(within(dialog).getByLabelText('New value 1')).toHaveValue('0.7')
    fireEvent.change(within(dialog).getByLabelText('New value 1'), { target: { value: '0.2' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Start clone' }))

    await waitFor(() => expect(router.state.location.pathname).toMatch(/^\/workbench\/run_MOCKCLONE\d+$/))
    const post = requests.find((r) => r.method === 'POST' && r.url.pathname === '/api/runs/run_MOCK02/clone')!
    expect(JSON.parse(post.body)).toEqual({ seed: 8, overrides: { '/agents/0/policy/llm/temperature': 0.2 } })
  })

  it('research: offers the seed only, and the request has no overrides', async () => {
    renderAt('/runs', <CloneDialog runId="run_MOCK01" onClose={() => {}} />)
    const dialog = await screen.findByRole('dialog')
    expect(await within(dialog).findByText(/a clone can change only the seed/)).toBeInTheDocument()
    expect(within(dialog).queryByRole('button', { name: /Add a change/ })).toBeNull()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Start clone' }))
    await waitFor(() => expect(requests.some((r) => r.method === 'POST')).toBe(true))
    expect(JSON.parse(requests.find((r) => r.method === 'POST')!.body)).toEqual({ seed: null })
  })
})
