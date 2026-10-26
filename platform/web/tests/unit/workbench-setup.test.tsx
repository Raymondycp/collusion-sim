import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { http, HttpResponse } from 'msw/http'
import { setupServer } from 'msw/node'
import { handlers } from '@/mocks/handlers'
import { mockConditions, mockSeeds, resetMockPresets, setMockResearchState } from '@/mocks/registry'
import { SetupPage } from '@/features/workbench/SetupPage'

// jsdom has no canvas: the scenario preview chart only needs an object that accepts calls.
vi.mock('lightweight-charts', () => {
  const any: unknown = new Proxy(() => any, { get: (_, k) => (k === 'then' ? undefined : any), apply: () => any })
  return { ColorType: {}, LineStyle: {}, LineType: {}, LineSeries: 'Line', HistogramSeries: 'Histogram', createSeriesMarkers: () => any, createChart: () => any }
})

const gets: string[] = []
const posts: string[] = []
const server = setupServer(...handlers)
server.events.on('request:start', ({ request }) => {
  const at = new URL(request.url).pathname
  if (request.method === 'GET') gets.push(at)
  if (request.method === 'POST') posts.push(at)
})
beforeAll(() => {
  server.listen({ onUnhandledFrame: 'bypass' })
})
afterAll(() => server.close())
afterEach(() => {
  cleanup()
  gets.length = 0
  posts.length = 0
  server.resetHandlers()
  resetMockPresets()
  setMockResearchState({})
})

function renderSetup() {
  const router = createMemoryRouter(
    [
      { path: '/workbench/new', element: <SetupPage /> },
      { path: '/workbench/:runId', element: <p>workbench run</p> },
      { path: '/runs', element: <p>runs page</p> },
    ],
    { initialEntries: ['/workbench/new'] },
  )
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return router
}

const pick = (id: string, value: string) => fireEvent.change(document.getElementById(id)!, { target: { value } })
const problem = (status: number, code: string, message: string) =>
  HttpResponse.json({ title: code, status, code, message, errors: [] }, { status, headers: { 'Content-Type': 'application/problem+json' } })

const startPosts = () => posts.filter((p) => p === '/api/runs').length

async function scripted() {
  await screen.findByRole('button', { name: 'Add seller' })
  pick('f-agents.0.policy.kind', 'constant')
  pick('f-agents.1.policy.kind', 'constant')
  pick('f-budget.max_calls', '10')
}

describe('workbench setup mode', () => {
  it('lays the form out as agent cards, a market preview and a Setting panel with Estimate and Start', async () => {
    renderSetup()
    await scripted()
    const agents = screen.getByRole('region', { name: 'Agents' })
    expect(agents.querySelector('ul')).toHaveClass('grid')
    expect(within(agents).getAllByRole('listitem').length).toBeGreaterThanOrEqual(2)
    const setting = screen.getByRole('region', { name: 'Setting' })
    expect(within(setting).getByRole('button', { name: /Estimate cost/ })).toBeInTheDocument()
    expect(setting.querySelector('#f-scenario_id')).not.toBeNull()
    expect(setting.querySelector('#f-agents\\.0\\.policy\\.kind')).toBeNull()
    const scenario = (document.getElementById('f-scenario_id') as HTMLSelectElement).value
    await waitFor(() => expect(gets).toContain(`/api/scenarios/${scenario}/reference`))
    expect(await within(screen.getByRole('region', { name: 'Market preview' })).findByText(new RegExp(`Scenario ${scenario} \\(the chosen scenario\\)`))).toBeInTheDocument()
  })

  it('starts only from the confirmation dialog, and opens the new run in the workbench', async () => {
    const router = renderSetup()
    await scripted()
    fireEvent.click(screen.getByRole('button', { name: /Estimate cost/ }))
    await screen.findByText(/Maximum cost/)
    fireEvent.click(screen.getByRole('button', { name: 'Start 1 run' }))
    const dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveTextContent('Every agent is scripted, so the runs make no model calls.')
    expect(startPosts()).toBe(0)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(startPosts()).toBe(0)
    fireEvent.click(screen.getByRole('button', { name: 'Start 1 run' }))
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Start' }))
    await waitFor(() => expect(router.state.location.pathname).toMatch(/^\/workbench\/run_MOCKNEW\d+_0$/))
    expect(startPosts()).toBe(1)
  })

  it('with LLM sellers: Start waits for the estimate, the dialog names the model calls and the caps, and an edit makes the estimate stale', async () => {
    renderSetup()
    await screen.findByRole('button', { name: 'Add seller' })
    expect((document.getElementById('f-agents.0.policy.kind') as HTMLSelectElement).value).toBe('llm')
    pick('f-budget.max_usd', '0.5')
    const startBtn = screen.getByRole('button', { name: 'Start 1 run' })
    expect(startBtn).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: /Estimate cost/ }))
    await screen.findByText(/Maximum cost/)
    expect(startBtn).toBeEnabled()
    fireEvent.click(startBtn)
    const dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveTextContent(/LLM agents call their providers\. Each run stops at its budget \(\$0\.5\)/)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    pick('f-budget.max_usd', '0.6')
    expect(screen.getByRole('button', { name: 'Start 1 run' })).toBeDisabled()
    expect(screen.getByText(/The form changed after the estimate/)).toBeInTheDocument()
    expect(startPosts()).toBe(0)
  })

  it('the + card adds a seller, a seller cannot be removed below two, and the Host card holds the host and the news', async () => {
    renderSetup()
    await scripted()
    const agents = screen.getByRole('region', { name: 'Agents' })
    const removeA = within(agents).getByRole('button', { name: 'Remove agent A' })
    expect(removeA).toBeDisabled()
    expect(removeA).toHaveAttribute('title', 'A run needs at least 2 sellers. The server refuses fewer.')
    fireEvent.click(within(agents).getByRole('button', { name: 'Add seller' }))
    await waitFor(() => expect(document.getElementById('f-agents.2.policy.kind')).not.toBeNull())
    expect(within(agents).getByRole('button', { name: 'Remove agent A' })).toBeEnabled()
    const host = within(agents).getByRole('region', { name: 'Host' })
    expect(within(host).getByText('No host agent: nobody posts the news.')).toBeInTheDocument()
    fireEvent.click(within(host).getByRole('button', { name: 'Add host' }))
    await waitFor(() => expect(within(host).getByDisplayValue('host')).toBeInTheDocument())
    expect(within(host).getByRole('combobox', { name: 'Policy' })).toHaveValue('scripted_host')
    const scenario = (document.getElementById('f-scenario_id') as HTMLSelectElement).value
    expect(await within(host).findByText(`News events of scenario ${scenario}`)).toBeInTheDocument()
    await waitFor(() => expect(within(host).getAllByRole('listitem').length).toBeGreaterThan(1))
    expect(within(host).getAllByText(/\(seen by /).length).toBeGreaterThan(0)
  })

  it('research: the condition fields are read only, and the preview shows the scenario of the first seed', async () => {
    renderSetup()
    fireEvent.click(await screen.findByRole('radio', { name: 'Research' }))
    await waitFor(() => expect(document.querySelector('#f-condition_id option[value="C3-accept"]')).not.toBeNull())
    pick('f-condition_id', 'C3-accept')
    const setting = screen.getByRole('region', { name: 'Setting' })
    for (const id of ['f-scenario_id', 'f-channels', 'f-call_mode', 'f-transparency', 'f-observation_mode']) expect(document.getElementById(id)).toBeNull()
    expect(within(setting).getAllByText('Observation mode').length).toBeGreaterThan(0)
    expect(within(setting).getAllByText('Call mode').length).toBeGreaterThan(0)
    expect(document.getElementById('f-agents.0.policy.llm.system_prompt')).toBeNull()
    fireEvent.click(await screen.findByRole('checkbox', { name: String(mockSeeds.pilot[0]) }))
    // The seed map's scenarios are not in the mock scenario list, so the preview names the scenario and says why it has none.
    const first = mockSeeds.seeds!.find((s) => s.seed === mockSeeds.pilot[0])!
    await waitFor(() => expect(gets).toContain(`/api/scenarios/${first.scenario_id}/reference`))
    expect(await within(screen.getByRole('region', { name: 'Market preview' })).findByText(new RegExp(`scenario ${first.scenario_id} \\(seed ${first.seed};`))).toBeInTheDocument()
  })

  // UI-WORKBENCH.md §3: Start goes through the server gates. The page shows each refusal and never starts a run around it.
  it.each([
    ['estimate', 409, 'condition_not_frozen', 'Condition C1 is a draft. Dev seeds need a frozen condition; use pilot seeds.'],
    ['estimate', 409, 'test_split_locked', 'Test seeds are locked until the freeze.'],
    ['estimate', 409, 'hash_mismatch', 'Condition C1 changed after the freeze.'],
    ['start', 409, 'test_seed_already_run', 'Test cell C1 seed 5 is used by run run_X. A test cell runs once; rerun it as exploratory.'],
    // The client shows its own sentence for this code (api/client.ts).
    ['start', 409, 'paid_calls_disabled', 'Paid calls are off on this server. The lead can turn them on (SIM_ALLOW_PAID=1).'],
  ] as const)('a %s refusal %i %s shows its code and message and starts nothing', async (step, status, code, message) => {
    const router = renderSetup()
    await scripted()
    server.use(http.post(step === 'estimate' ? '/api/runs/estimate' : '/api/runs', () => problem(status, code, message)))
    fireEvent.click(screen.getByRole('button', { name: /Estimate cost/ }))
    if (step === 'start') {
      await screen.findByText(/Maximum cost/)
      fireEvent.click(screen.getByRole('button', { name: 'Start 1 run' }))
      fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Start' }))
    }
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(`${status} ${code}:`)
    expect(alert).toHaveTextContent(message)
    expect(startPosts()).toBe(step === 'start' ? 1 : 0)
    expect(router.state.location.pathname).toBe('/workbench/new')
    if (step === 'estimate') expect(screen.getByRole('button', { name: /Start 1 run/ })).toBeDisabled()
  })

  it('research: test seeds stay off until the freeze, as on the New run page', async () => {
    setMockResearchState({ conditions: mockConditions.map((c) => (c.id === 'C2a' ? { ...c, status: 'frozen' as const, frozen_hash: c.hash } : c)) })
    renderSetup()
    fireEvent.click(await screen.findByRole('radio', { name: 'Research' }))
    await waitFor(() => expect(document.querySelector('#f-condition_id option[value="C2a"]')).not.toBeNull())
    pick('f-condition_id', 'C2a')
    const split = document.getElementById('f-split') as HTMLSelectElement
    expect([...split.options].find((o) => o.value === 'test')).toBeDisabled()
    expect([...split.options].find((o) => o.value === 'dev')).not.toBeDisabled()
  })
})
