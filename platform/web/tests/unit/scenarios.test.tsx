import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { http, HttpResponse } from 'msw/http'
import { setupServer } from 'msw/node'
import { ScenariosPage } from '@/features/scenarios/ScenariosPage'
import { ScenarioPage } from '@/features/scenarios/ScenarioPage'
import { scenarioSeries } from '@/features/scenarios/ScenarioChart'
import { draftDiff, markersFor, resizeSellers } from '@/features/scenarios/scenario'
import { handlers } from '@/mocks/handlers'
import { resetMockScenarios } from '@/mocks/scenarios'
import { fixtureGenerators } from '@/mocks/scenarioFixtures'
import type { ScenarioDraft } from '@/api/scenarios'

// jsdom has no canvas: the chart library becomes a stub, and the chart's aria-label carries what it draws.
vi.mock('lightweight-charts', () => {
  const series = { setData() {}, applyOptions() {}, priceScale: () => ({ applyOptions() {} }) }
  return {
    ColorType: { Solid: 'solid' },
    LineStyle: { Solid: 0, Dotted: 1, Dashed: 2 },
    LineType: { Simple: 0, WithSteps: 1 },
    LineSeries: 'Line',
    HistogramSeries: 'Histogram',
    createSeriesMarkers: () => ({ setMarkers() {} }),
    createChart: () => ({ applyOptions() {}, addSeries: () => series, removeSeries() {}, panes: () => [], timeScale: () => ({ fitContent() {} }), remove() {} }),
  }
})

const posts: { path: string; method: string; body: unknown }[] = []
const server = setupServer(...handlers)
server.events.on('request:start', async ({ request }) => {
  if (request.method !== 'GET') posts.push({ path: new URL(request.url).pathname, method: request.method, body: request.body ? await request.clone().json().catch(() => null) : null })
})
beforeAll(() => server.listen({ onUnhandledFrame: 'bypass' }))
afterAll(() => server.close())
afterEach(() => {
  cleanup()
  posts.length = 0
  server.resetHandlers()
  resetMockScenarios()
})

function renderAt(url: string) {
  const router = createMemoryRouter(
    [
      { path: '/scenarios', element: <ScenariosPage /> },
      { path: '/scenarios/new', element: <ScenarioPage /> },
      { path: '/scenarios/:scenarioId', element: <ScenarioPage /> },
    ],
    { initialEntries: [url] },
  )
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return router
}

const field = (id: string) => document.getElementById(id) as HTMLInputElement
const type = (id: string, value: string | number) => fireEvent.change(field(id), { target: { value: String(value) } })
const sent = (path: string, method = 'POST') => posts.filter((p) => p.path === path && p.method === method)

describe('scenario list', () => {
  it('lists every scenario with its status, and the status filter asks the server', async () => {
    renderAt('/scenarios')
    expect(await screen.findByRole('link', { name: 'Cost shock at round 10' })).toHaveAttribute('href', '/scenarios/scn_cost_shock_v1')
    expect(screen.getAllByRole('row')).toHaveLength(5)
    fireEvent.click(screen.getByRole('radio', { name: 'Drafts' }))
    await waitFor(() => expect(screen.getAllByRole('row')).toHaveLength(2))
    expect(screen.getByRole('link', { name: 'Entry of a third seller (draft)' })).toBeInTheDocument()
  })
})

describe('frozen scenario', () => {
  it('is read-only and shows its hash, approver and provenance', async () => {
    renderAt('/scenarios/scn_cost_shock_v1')
    expect(await screen.findByText('sha256:9f2c…e1')).toBeInTheDocument()
    expect(screen.getByText('mockgen')).toBeInTheDocument()
    expect(screen.getByText(/"generator_id": "template"/)).toBeInTheDocument()
    expect(screen.queryAllByRole('textbox')).toEqual([])
    expect(screen.queryAllByRole('spinbutton')).toEqual([])
    expect(screen.queryByRole('button', { name: /Save|Freeze|Delete/ })).toBeNull()
    expect(await screen.findByRole('img', { name: /30 rounds.*changing params a, c.*7 event markers.*quiet window R18 to R25/ })).toBeInTheDocument()
  })

  it('"Clone to draft" posts an editable copy without id, status or hash, then opens it', async () => {
    const router = renderAt('/scenarios/scn_cost_shock_v1')
    fireEvent.click(await screen.findByRole('button', { name: 'Clone to draft' }))
    await waitFor(() => expect(router.state.location.pathname).toMatch(/^\/scenarios\/scn_MOCKNEW\d+$/))
    const body = sent('/api/scenarios')[0]?.body as Record<string, unknown>
    expect(body).not.toHaveProperty('id')
    expect(body).not.toHaveProperty('status')
    expect(body).not.toHaveProperty('hash')
    expect(body).toMatchObject({ name: 'Cost shock at round 10 (copy)', provenance: { cloned_from: 'scn_cost_shock_v1', cloned_from_hash: 'sha256:9f2c…e1' } })
    expect(await screen.findByRole('button', { name: 'Save' })).toBeDisabled()
    expect(field('scn-name').value).toBe('Cost shock at round 10 (copy)')
  })
})

describe('new draft', () => {
  it('chooses no demand model for the person; picking one builds the params form from params_schema', async () => {
    renderAt('/scenarios/new')
    const start = await screen.findByLabelText('Start from a demand model')
    expect(start).toHaveValue('')
    fireEvent.change(start, { target: { value: 'linear' } })
    for (const k of ['a', 'b', 'c', 'd']) expect(field(`f-params.${k}`)).toHaveValue(null)
    expect(screen.getByText('q_i = a − b·p_i + d·mean(p_−i)')).toBeInTheDocument()
    expect(await screen.findByRole('alert')).toHaveTextContent(/No preview: .*market\.demand: .*Field required/)
  })

  it('the demand helper turns target prices into params, and the preview follows', async () => {
    renderAt('/scenarios/new')
    fireEvent.change(await screen.findByLabelText('Start from a demand model'), { target: { value: 'linear' } })
    fireEvent.click(screen.getByText(/Demand helper/))
    type('f-solver.nash', 5.75)
    type('f-solver.joint_optimum', 10.5)
    fireEvent.click(screen.getByRole('button', { name: 'Solve' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Use these params' }))
    expect(field('f-params.a')).toHaveValue(10)
    expect(field('f-params.d')).toHaveValue(1)
    expect(await screen.findByRole('img', { name: /Preview of 30 rounds/ })).toBeInTheDocument()
  })

  it('a new event starts as a valid parameter shock, reaches the preview request and adds markers', async () => {
    renderAt('/scenarios/scn_draft_entry')
    fireEvent.click(await screen.findByRole('button', { name: 'Add event' }))
    const ev = screen.getByRole('group', { name: 'Event 2' })
    type('ev1-announce', 12)
    type('ev1-effect', 13)
    expect(within(ev).getAllByRole('button', { name: 'Remove shock' })).toHaveLength(1)
    expect(field('ev1-s0-path')).toHaveValue('/c')
    type('ev1-s0-value', 0.5)
    await waitFor(
      () => {
        const last = sent('/api/market/preview').at(-1)?.body as { events: { shocks: unknown[] }[] } | undefined
        expect(last?.events[1]).toMatchObject({ announce: 12, effect: 13, shocks: [{ path: '/c', op: 'add', value: 0.5 }] })
      },
    )
    expect(await screen.findByRole('img', { name: /changing params a, c, 6 event markers/ })).toBeInTheDocument()
  })
})

describe('generate dialog', () => {
  const open = async (url = '/scenarios/scn_draft_entry') => {
    renderAt(url)
    fireEvent.click(await screen.findByRole('button', { name: 'Generate' }))
    return screen.findByRole('dialog')
  }
  const runTemplate = async (dialog: HTMLElement) => {
    fireEvent.change(within(dialog).getByLabelText('Generator'), { target: { value: 'template' } })
    expect(field('f-inputs.template')).toHaveValue('')
    expect(field('f-inputs.min_rounds')).toHaveValue(30)
    fireEvent.change(field('f-inputs.template'), { target: { value: '0' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Generate' }))
    return within(dialog).findByRole('list', { name: 'Check of the generated draft' })
  }

  it('chooses no generator, and a paid generator cannot be picked', async () => {
    server.use(http.get('/api/scenario-generators', () => HttpResponse.json([...fixtureGenerators, { id: 'llm', version: '1', name: 'LLM writer', input_schema: { type: 'object' }, paid: true }])))
    const dialog = await open()
    const pick = within(dialog).getByLabelText('Generator')
    expect(pick).toHaveValue('')
    expect(within(dialog).getByRole('option', { name: /LLM writer \(costs LLM calls: needs lead approval\)/ })).toBeDisabled()
  })

  it('shows the check and the diff; Discard keeps the draft and saves nothing', async () => {
    const dialog = await open()
    const checks = await runTemplate(dialog)
    expect(within(checks).getAllByRole('listitem')).toHaveLength(6)
    expect(within(dialog).getByText(/Changes to the current draft/)).toBeInTheDocument()
    expect(within(dialog).getByRole('cell', { name: 'name' })).toBeInTheDocument()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Discard' }))
    expect(field('scn-name').value).toBe('Entry of a third seller (draft)')
    expect(sent('/api/scenarios/scn_draft_entry', 'PUT')).toEqual([])
  })

  it('"Edit in the form" loads the draft unsaved; "Accept and save" stores it', async () => {
    let dialog = await open()
    await runTemplate(dialog)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Edit in the form' }))
    expect(field('scn-name').value).toBe('Generated scenario')
    expect(screen.getByText('unsaved changes')).toBeInTheDocument()
    expect(sent('/api/scenarios/scn_draft_entry', 'PUT')).toEqual([])

    fireEvent.click(screen.getByRole('button', { name: 'Generate' }))
    dialog = await screen.findByRole('dialog')
    await runTemplate(dialog)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Accept and save' }))
    await waitFor(() => expect(sent('/api/scenarios/scn_draft_entry', 'PUT')).toHaveLength(1))
    await waitFor(() => expect(screen.queryByText('unsaved changes')).toBeNull())
  })
})

describe('draft: save, check and freeze', () => {
  it('each accepted Save counts one more edit (Scenario.version, contract 0.8.0)', async () => {
    renderAt('/scenarios/scn_draft_entry')
    expect(await screen.findByText(/Saved edit 1\./)).toBeInTheDocument()
    for (const [name, edit] of [['A', 2], ['B', 3]] as const) {
      fireEvent.change(screen.getByLabelText('Name'), { target: { value: `Renamed ${name}` } })
      fireEvent.click(screen.getByRole('button', { name: 'Save' }))
      expect(await screen.findByText(new RegExp(`Saved edit ${edit}\\.`))).toBeInTheDocument()
    }
  })

  it('a 422 field error with a slash path lands on its event', async () => {
    renderAt('/scenarios/scn_draft_entry')
    type(await screen.findByLabelText('News in round').then((e) => e.id), 9)
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    const ev = screen.getByRole('group', { name: 'Event 1' })
    expect(await within(ev).findByRole('alert')).toHaveTextContent('effect must not come before the announcement')
  })

  it('Check and Freeze wait for Save; a failed check blocks the freeze and names the check', async () => {
    renderAt('/scenarios/scn_draft_entry')
    const check = await screen.findByRole('button', { name: 'Check' })
    type('ev0-announce', 20)
    type('ev0-effect', 20)
    expect(check).toBeDisabled()
    type('scn-approver', 'Raymond')
    expect(screen.getByRole('button', { name: 'Freeze' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Check' })).toBeEnabled())
    expect(field('scn-approver').value).toBe('Raymond')
    fireEvent.click(screen.getByRole('button', { name: 'Freeze' }))
    expect(await screen.findByText('checks.quiet_window')).toBeInTheDocument()
    expect(sent('/api/scenarios/scn_draft_entry/freeze')).toHaveLength(1)
    expect(screen.getByRole('button', { name: 'Save' })).toBeInTheDocument()
  })

  it('a passing draft freezes and becomes read-only', async () => {
    renderAt('/scenarios/scn_draft_entry')
    type(await screen.findByLabelText('Approved by').then((e) => e.id), 'Raymond')
    fireEvent.click(screen.getByRole('button', { name: 'Freeze' }))
    expect(await screen.findByRole('button', { name: 'Clone to draft' })).toBeInTheDocument()
    expect(screen.getByText('Raymond')).toBeInTheDocument()
    expect(sent('/api/scenarios/scn_draft_entry/freeze')[0]!.body).toEqual({ approved_by: 'Raymond' })
  })
})

describe('preview data', () => {
  const rounds = [1, 2, 3, 4].map((t) => ({ t, params: { a: 10, b: 1.5, c: t >= 3 ? 1.5 : 1, c_offset: {} }, costs: {}, nash: t >= 3 ? 6.125 : 5.75, joint_optimum: 10.5 }))
  it('draws only params that change, the quiet rounds and one marker per event step', () => {
    const events: ScenarioDraft['events'] = [{ kind: 'param_shock', announce: 2, effect: 3, length: 2, shocks: [{ path: '/c', op: 'add', value: 0.5 }], visible: ['A'] }]
    const s = scenarioSeries(rounds, markersFor(events), [2, 3])
    expect(s.params.map((p) => p.name)).toEqual(['c'])
    expect(s.quiet).toEqual([2, 3])
    expect(s.markers).toEqual([
      { t: 2, text: 'News 1' },
      { t: 3, text: 'Shock 1' },
      { t: 5, text: 'End 1' },
    ])
    expect(s.nash.map((p) => p.value)).toEqual([5.75, 5.75, 6.125, 6.125])
  })

  it('a news-only event has an announce marker only', () => {
    expect(markersFor([{ kind: 'news_only', announce: 7, effect: null, length: null, visible: ['A'] }])).toEqual([{ t: 7, kind: 'announce', event_index: 0 }])
  })

  it('the diff lists changed leaves only', () => {
    const a = { name: 'x', market: { demand: { id: 'linear', params: { a: 10, b: 1.5 } } }, n_sellers: 2, horizon: { min_rounds: 30, max_rounds: 30, stop_prob: 0 }, events: [] } as ScenarioDraft
    const b = structuredClone(a)
    b.market.demand.params.a = 11
    expect(draftDiff(a, b)).toEqual([{ path: 'market.demand.params.a', before: 10, after: 11 }])
  })
})

describe('CR-10 fixes', () => {
  it('M1: the quiet window has no off switch, a new draft starts at 18–25, and an edit sends both values', async () => {
    renderAt('/scenarios/new')
    fireEvent.change(await screen.findByLabelText('Start from a demand model'), { target: { value: 'linear' } })
    expect(screen.queryByRole('checkbox', { name: /no change in these rounds/ })).toBeNull()
    expect(field('scn-quiet-from')).toHaveValue(18)
    expect(field('scn-quiet-to')).toHaveValue(25)
    for (const [k, v] of [['a', 10], ['b', 1.5], ['c', 1], ['d', 1]] as const) type(`f-params.${k}`, v)
    type('scn-quiet-to', 26)
    fireEvent.click(screen.getByRole('button', { name: 'Save draft' }))
    await waitFor(() => expect(sent('/api/scenarios')).toHaveLength(1))
    expect(sent('/api/scenarios')[0]!.body).toMatchObject({ quiet_window: [18, 26] })
  })

  it('m1: a parameter shock without shocks is refused like the backend does, and the event shows why', async () => {
    renderAt('/scenarios/scn_draft_entry')
    const ev = await screen.findByRole('group', { name: 'Event 1' })
    fireEvent.click(within(ev).getByRole('button', { name: 'Remove shock' }))
    expect(within(ev).getByText(/needs at least one shock/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await within(ev).findByRole('alert')).toHaveTextContent('a parameter shock needs shocks, effect and length')
  })

  it('m1: the events table gives the shock length, never "to the end"', async () => {
    renderAt('/scenarios/scn_cost_shock_v1')
    expect(await screen.findByText('R10 for 5')).toBeInTheDocument()
    expect(screen.queryByText(/to the end/)).toBeNull()
  })

  it('m2: a clone keeps the provenance of its source', async () => {
    renderAt('/scenarios/scn_cost_shock_v1')
    fireEvent.click(await screen.findByRole('button', { name: 'Clone to draft' }))
    await waitFor(() => expect(sent('/api/scenarios')).toHaveLength(1))
    expect(sent('/api/scenarios')[0]!.body).toMatchObject({ provenance: { cloned_from: 'scn_cost_shock_v1', source_provenance: { generator_id: 'template', seed: 0 } } })
  })

  it('m3: Check names the event whose news disagrees with its shock, and the quiet window must end before min_rounds', async () => {
    renderAt('/scenarios/scn_draft_entry')
    const news = (await screen.findByLabelText('News text')) as HTMLTextAreaElement
    fireEvent.change(news, { target: { value: 'Demand rises: a new rival opens nearby.' } })
    type('scn-quiet-to', 30)
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Check' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: 'Check' }))
    const report = await screen.findByRole('list', { name: 'Check' })
    expect(report).toHaveTextContent('Event 0: the shocks give a fall, but the news text gives a rise')
    expect(report).toHaveTextContent('Quiet window ends at 30, not before min_rounds 30')
  })

  it('m4: an error that is not an API error shows a message instead of failing silently', async () => {
    server.use(http.post('/api/scenarios', () => HttpResponse.json(null, { status: 201 })))
    vi.spyOn(console, 'error').mockImplementation(() => {})
    renderAt('/scenarios/scn_cost_shock_v1')
    fireEvent.click(await screen.findByRole('button', { name: 'Clone to draft' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Unexpected error: see the browser console.')
    vi.mocked(console.error).mockRestore()
  })

  it('m5: clearing a number field leaves it empty while typing, and the typed value is saved', async () => {
    renderAt('/scenarios/scn_draft_entry')
    const max = (await screen.findByLabelText('Max rounds')) as HTMLInputElement
    fireEvent.focus(max)
    fireEvent.change(max, { target: { value: '' } })
    expect(max.value).toBe('')
    fireEvent.blur(max)
    expect(max.value).toBe('')
    fireEvent.focus(max)
    fireEvent.change(max, { target: { value: '45' } })
    fireEvent.blur(max)
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(sent('/api/scenarios/scn_draft_entry', 'PUT')).toHaveLength(1))
    expect(sent('/api/scenarios/scn_draft_entry', 'PUT')[0]!.body).toMatchObject({ horizon: { max_rounds: 45 } })
  })

  it('m5: changing the seller count keeps the existing ids that are still in range', () => {
    expect(resizeSellers(['X', 'Y', 'Z'], 2)).toEqual(['X', 'Y'])
    expect(resizeSellers(['X', 'A'], 3)).toEqual(['X', 'A', 'B'])
  })
})
