import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { setupServer } from 'msw/node'
import Ajv2020 from 'ajv/dist/2020'
import { load } from 'js-yaml'
import { DEFAULT_BRANCH_PLAN, type Experiment, type ExperimentCreate } from '@/api/experiments'
import { ExperimentPage, ExperimentsPage } from '@/features/experiments/ExperimentsPage'
import { NewExperimentPage } from '@/features/experiments/NewExperimentPage'
import { experimentRequest, plannedChildren, runsOf } from '@/features/experiments/plan'
import { handlers } from '@/mocks/handlers'
import { blockMockCell, resetMockExperiments } from '@/mocks/experiments'
import { setMockPaidCalls } from '@/mocks/providers'
import { mockConditions, mockRunConfigSchema, resetMockPresets } from '@/mocks/registry'

const contract = load(readFileSync(resolve(__dirname, '../../../docs/sim/api/openapi.yaml'), 'utf8')) as { components: { schemas: Record<string, { properties: Record<string, { default?: unknown }> }> } }
const ajv = new Ajv2020({ strict: false, allErrors: true, validateFormats: false })
ajv.addSchema(contract as object, 'openapi')
const valid = (name: string, value: unknown) => {
  const v = ajv.getSchema(`openapi#/components/schemas/${name}`)!
  return v(value) ? [] : [ajv.errorsText(v.errors)]
}

const bodies: { url: string; body: string }[] = []
const server = setupServer(...handlers)
server.events.on('request:start', async ({ request }) => {
  if (request.method === 'POST') bodies.push({ url: request.url, body: await request.clone().text() })
})
beforeAll(() => server.listen({ onUnhandledFrame: 'bypass' }))
afterAll(() => server.close())
afterEach(() => {
  cleanup()
  bodies.length = 0
  resetMockExperiments()
  resetMockPresets()
  setMockPaidCalls(false)
})

function renderAt(path: string) {
  const router = createMemoryRouter(
    [
      { path: '/experiments', element: <ExperimentsPage /> },
      { path: '/experiments/new', element: <NewExperimentPage /> },
      { path: '/experiments/:experimentId', element: <ExperimentPage /> },
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

const cond = (id: string) => mockConditions.find((c) => c.id === id)!
const pick = (id: string, value: string) => fireEvent.change(document.getElementById(id)!, { target: { value } })
const lastBody = (path: string) => JSON.parse(bodies.filter((b) => b.url.endsWith(path)).at(-1)!.body) as ExperimentCreate

const scripted = (ids: string[]) => ids.map((id) => ({ id, role: 'seller', policy: { kind: 'constant', params: { price: 5 } }, capability: null }))
const body = (over: Partial<ExperimentCreate> = {}): ExperimentCreate => ({
  conditions: ['C1', 'C2a'],
  split: 'pilot',
  seeds: [1, 2],
  agents: scripted(['A', 'B']) as never,
  budget: { max_usd: 0.5 },
  batch_budget: { max_usd: 5 },
  branch_plan: structuredClone(DEFAULT_BRANCH_PLAN),
  ...over,
})
const post = async (path: string, b: ExperimentCreate) => {
  const r = await fetch(`${location.origin}/api${path}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b) })
  return { status: r.status, json: (await r.json()) as Experiment & { errors?: { path: string }[] } }
}

async function chooseCells() {
  // The mock conditions are all drafts, so the page opens on its empty state first (UX review 3, item 4).
  fireEvent.click(await screen.findByRole('button', { name: 'Plan a pilot experiment on drafts' }))
  fireEvent.click(await screen.findByRole('checkbox', { name: /^C1 / }))
  fireEvent.click(screen.getByRole('checkbox', { name: /^C2a / }))
  await waitFor(() => expect(screen.getByRole('checkbox', { name: '1' })).toBeInTheDocument())
  fireEvent.click(screen.getByRole('checkbox', { name: '1' }))
  fireEvent.click(screen.getByRole('checkbox', { name: '2' }))
}

describe('experiment plan', () => {
  it('the branch plan defaults are the contract defaults', () => {
    const s = contract.components.schemas
    const defaults = (name: string) => Object.fromEntries(Object.entries(s[name]!.properties).map(([k, p]) => [k, p.default]))
    expect(DEFAULT_BRANCH_PLAN).toEqual({ probe: defaults('ProbePlan'), placebo: defaults('PlaceboPlan'), ablation: defaults('AblationPlan') })
  })

  it('children per cell: probe, three placebos, and an ablation per private channel, or one null ablation without private channels', () => {
    const kinds = (id: string) => plannedChildren(DEFAULT_BRANCH_PLAN, cond(id)).map((c) => c.channel ?? c.kind)
    expect(kinds('C1')).toEqual(['probe', 'placebo', 'private_A_B'])
    expect(kinds('C3-accept')).toEqual(['probe', 'placebo', 'private_A_B'])
    expect(kinds('C2a')).toEqual(['probe', 'placebo', 'ablation'])
    expect(plannedChildren(DEFAULT_BRANCH_PLAN, cond('C2a')).at(-1)).toMatchObject({ kind: 'ablation', channel: null, rounds: null, n: 1 })
    expect(runsOf({ children: plannedChildren({ ...DEFAULT_BRANCH_PLAN, ablation: { ...DEFAULT_BRANCH_PLAN.ablation, groups: ['C1'] } }, cond('C2a')) })).toBe(5)
    expect(runsOf({ children: plannedChildren(DEFAULT_BRANCH_PLAN, cond('C1')) })).toBe(6)
    expect(runsOf({ children: plannedChildren({ ...DEFAULT_BRANCH_PLAN, ablation: { ...DEFAULT_BRANCH_PLAN.ablation, groups: ['C3'] } }, cond('C1')) })).toBe(5)
    expect(runsOf({ children: plannedChildren({ probe: null, placebo: null, ablation: null }, cond('C1')) })).toBe(1)
    const noPairs = { ...cond('C1'), channels: { ...cond('C1').channels, private_pairs: false } }
    expect(plannedChildren(DEFAULT_BRANCH_PLAN, noPairs).map((c) => c.channel ?? c.kind)).toEqual(['probe', 'placebo', 'ablation'])
  })

  it('the request nulls condition-set agent fields in every agent list and takes memory.summary from the conditions', () => {
    const dirty = ['A', 'B'].map((id) => ({
      id,
      role: 'seller',
      capability: { grants: [] },
      policy: { kind: 'llm' as const, llm: { provider_profile_id: 'prof_mock', model: 'm', system_prompt: 'x', talk_prompt: 'y' } },
    }))
    const d = body({ agents: dirty, agents_by_condition: { C2a: dirty }, memory: { summary: 'llm' } as never })
    const out = experimentRequest(d, [cond('C1'), cond('C2a')], mockRunConfigSchema, ['C1', 'C2a'])
    const locked = (a: ExperimentCreate['agents']) => a.map((x) => [x.capability, x.policy.llm?.system_prompt ?? null, x.policy.llm?.talk_prompt ?? null])
    expect([...locked(out.agents), ...locked(out.agents_by_condition!.C2a!)].flat()).toEqual(Array(12).fill(null))
    expect(out.memory?.summary).toBe('none')
  })

  it("the mock's estimate, experiment and summary match the contract schemas", async () => {
    const e = await post('/experiments/estimate', body())
    expect(e.status).toBe(200)
    expect(valid('ExperimentEstimate', e.json)).toEqual([])
    const x = await post('/experiments', body())
    expect(x.status).toBe(202)
    expect(valid('Experiment', x.json)).toEqual([])
    blockMockCell(x.json.id, 'C1', 1, 'hash_mismatch', 'scn_s1 changed')
    const detail = await (await fetch(`${location.origin}/api/experiments/${x.json.id}`)).json()
    expect(valid('Experiment', detail)).toEqual([])
    const list = (await (await fetch(`${location.origin}/api/experiments`)).json()) as unknown[]
    expect(list.flatMap((s) => valid('ExperimentSummary', s))).toEqual([])
  })
})

describe('new experiment form', () => {
  it('plans the grid, estimates, and starts; the request holds no locked agent field and no label', async () => {
    setMockPaidCalls(true)
    const router = renderAt('/experiments/new')
    expect(screen.queryByRole('table', { name: 'Planned cells' })).toBeNull()
    await chooseCells()

    const grid = screen.getByRole('table', { name: 'Planned cells' })
    const rows = within(grid).getAllByRole('row').slice(1)
    expect(rows[0]).toHaveTextContent(/C1.*probe.*placebo.*ablation of private_A_B/)
    expect(rows[1]).toHaveTextContent(/C2a.*probe.*placebo.*null ablation from R10/)
    expect(rows[1]).not.toHaveTextContent('ablation of')
    expect(within(rows[0]!).getAllByRole('cell').map((c) => c.textContent)).toEqual([expect.stringMatching(/^v2.*6 runs$/), expect.stringMatching(/^v1.*6 runs$/)])
    expect(within(rows[1]!).getAllByRole('cell').map((c) => c.textContent)).toEqual([expect.stringMatching(/6 runs$/), expect.stringMatching(/6 runs$/)])
    expect(screen.getByRole('heading', { name: 'Plan: 4 cells, 24 runs' })).toBeInTheDocument()

    const startButton = () => screen.getByRole('button', { name: /Start experiment/ })
    expect(startButton()).toBeDisabled()
    pick('f-budget.max_usd', '0.5')
    pick('f-batch_budget.max_usd', '100')
    fireEvent.click(screen.getByRole('button', { name: /Estimate cost/ }))
    expect(await screen.findByText(/4 cells, 24 runs \(4 parents, 4 probe, 12 placebo, 4 ablation\)/)).toBeInTheDocument()
    expect(startButton()).toBeEnabled()

    pick('f-batch_budget.max_usd', '200')
    expect(await screen.findByText(/The form changed after the estimate/)).toBeInTheDocument()
    expect(startButton()).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: /Estimate cost/ }))
    await waitFor(() => expect(startButton()).toBeEnabled())

    fireEvent.click(startButton())
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByRole('heading')).toHaveTextContent('Paid start: 24 runs in 4 cells')
    expect(within(within(dialog).getByRole('list', { name: 'LLM agents' })).getAllByRole('listitem').map((li) => li.textContent)).toEqual([expect.stringMatching(/^A: .+, .+/), expect.stringMatching(/^B: .+, .+/)])
    const go = within(dialog).getByRole('button', { name: 'Start paid runs' })
    expect(go).toBeDisabled()
    fireEvent.click(within(dialog).getByRole('checkbox', { name: /Paid model calls: up to [\d,]+ calls, cost up to \$[\d.]+\. I have the lead's approval/ }))
    fireEvent.click(go)
    await waitFor(() => expect(router.state.location.pathname).toMatch(/^\/experiments\/exp_[0-9A-HJKMNP-TV-Z]{26}$/))

    const sent = lastBody('/api/experiments')
    expect(sent).toMatchObject({ conditions: ['C1', 'C2a'], split: 'pilot', seeds: [1, 2], batch_budget: { max_usd: 200 }, memory: { summary: 'none' } })
    expect(sent.agents.map((a) => [a.id, a.capability, a.policy.llm?.system_prompt ?? null, a.policy.llm?.talk_prompt ?? null])).toEqual([
      ['A', null, null, null],
      ['B', null, null, null],
    ])
    expect(JSON.stringify(sent)).not.toMatch(/"labels"|"legal"|"harm"/)
  })

  it('the ablation groups sent are the ticked ones among the groups shown, never C4 or LC', async () => {
    renderAt('/experiments/new')
    await chooseCells()
    const groups = screen.getByRole('group', { name: 'Groups' })
    const shown = within(groups).getAllByRole('checkbox').map((c) => c.closest('label')!.textContent)
    expect(shown).toEqual(['C1', 'C2a', 'C2b', 'C3'])
    pick('f-budget.max_usd', '0.5')
    pick('f-batch_budget.max_usd', '100')
    fireEvent.click(screen.getByRole('button', { name: /Estimate cost/ }))
    await waitFor(() => expect(bodies.some((b) => b.url.endsWith('/api/experiments/estimate'))).toBe(true))
    expect(lastBody('/api/experiments/estimate').branch_plan?.ablation?.groups).toEqual(shown)
    fireEvent.click(within(groups).getByRole('checkbox', { name: 'C2b' }))
    fireEvent.click(screen.getByRole('button', { name: /Estimate cost/ }))
    await waitFor(() => expect(lastBody('/api/experiments/estimate').branch_plan?.ablation?.groups).toEqual(['C1', 'C2a', 'C3']))
  })

  it('a condition override sends its own agents, cleaned, and drops when the condition is unchosen', async () => {
    renderAt('/experiments/new')
    await chooseCells()
    fireEvent.click(screen.getByRole('checkbox', { name: 'Other agents for C2a' }))
    pick('f-budget.max_usd', '0.5')
    pick('f-batch_budget.max_calls', '100000')
    fireEvent.click(screen.getByRole('button', { name: /Estimate cost/ }))
    await screen.findByText(/Maximum cost/)
    const sent = lastBody('/api/experiments/estimate')
    expect(Object.keys(sent.agents_by_condition ?? {})).toEqual(['C2a'])
    expect(sent.agents_by_condition!.C2a!.every((a) => a.capability === null && (a.policy.llm?.system_prompt ?? null) === null)).toBe(true)

    fireEvent.click(screen.getByRole('checkbox', { name: /^C2a / }))
    expect(screen.queryByRole('checkbox', { name: 'Other agents for C2a' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /Estimate cost/ }))
    await waitFor(() => expect(lastBody('/api/experiments/estimate').agents_by_condition).toEqual({}))
  })

  it('shows the server refusals: 422 by field, and 409 while paid calls are off', async () => {
    setMockPaidCalls(false)
    renderAt('/experiments/new')
    await chooseCells()
    pick('f-budget.max_usd', '0.5')
    fireEvent.click(screen.getByRole('button', { name: /Estimate cost/ }))
    expect(await screen.findByText(/batch_budget.max_usd: Value error, set at least one of max_usd, max_calls and max_tokens/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Start experiment/ })).toBeDisabled()

    pick('f-batch_budget.max_usd', '100')
    pick('f-branch_plan.placebo.at', '28')
    fireEvent.click(screen.getByRole('button', { name: /Estimate cost/ }))
    expect(await screen.findByText(/branch_plan.placebo: C1: rounds 28–32 pass the last round 30; C2a: rounds 28–32 pass the last round 30/)).toBeInTheDocument()

    pick('f-branch_plan.placebo.at', '20')
    fireEvent.click(screen.getByRole('button', { name: /Estimate cost/ }))
    await screen.findByText(/Maximum cost/)
    fireEvent.click(screen.getByRole('button', { name: /Start experiment/ }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('checkbox', { name: /I have the lead's approval/ }))
    fireEvent.click(within(dialog).getByRole('button', { name: 'Start paid runs' }))
    expect(await screen.findByText(/Paid calls are off on this server/)).toBeInTheDocument()
  })

  it('with no frozen condition the page shows an empty state with a link to Settings > Freeze; the pilot way on opens the form', async () => {
    renderAt('/experiments/new')
    expect(await screen.findByText('No frozen conditions are available.')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Settings > Freeze/ })).toHaveAttribute('href', '/settings/freeze')
    expect(screen.getByText(/stage 1 \(conditions\) of the freeze/)).toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: /^C1 / })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Plan a pilot experiment on drafts' }))
    expect(await screen.findByRole('checkbox', { name: /^C1 / })).toBeInTheDocument()
    expect(screen.queryByText('No frozen conditions are available.')).toBeNull()
  })

  it('on drafts only, the dev and test splits stay locked and name why (CR-103 note 3)', async () => {
    renderAt('/experiments/new')
    fireEvent.click(await screen.findByRole('button', { name: 'Plan a pilot experiment on drafts' }))
    await screen.findByRole('checkbox', { name: /^C1 / })
    fireEvent.click(screen.getByRole('checkbox', { name: /^C1 / }))
    const split = document.getElementById('f-split') as HTMLSelectElement
    const opt = (v: string) => [...split.options].find((o) => o.value === v)!
    expect(opt('pilot')).not.toBeDisabled()
    expect(opt('dev')).toBeDisabled()
    expect(opt('dev')).toHaveTextContent(/draft; dev needs a frozen condition/)
    expect(opt('test')).toBeDisabled()
    expect(opt('test')).toHaveTextContent('locked until the freeze')
  })

  it('Estimate stays off with no per-run cap, with one line that says so, and turns on when a cap is set (UX review 3, item 5)', async () => {
    renderAt('/experiments/new')
    await chooseCells()
    const budget = document.getElementById('f-budget.max_usd')!.closest('fieldset')!
    const estimate = screen.getByRole('button', { name: /Estimate cost/ })
    expect(estimate).toBeDisabled()
    expect(within(budget).getByRole('note')).toHaveTextContent('No per-run cap set. Add Max calls, Max tokens, or Max USD before estimating.')
    pick('f-budget.max_usd', '0.5')
    expect(estimate).toBeEnabled()
    expect(within(budget).getAllByRole('note')).toHaveLength(1)
    expect(within(budget).getByRole('note')).toHaveTextContent(/^Per-run caps set: \$0\.5\./)
  })

  it('a scripted experiment says it makes no model calls and starts with no approval box', async () => {
    const router = renderAt('/experiments/new')
    await chooseCells()
    pick('f-agents.0.policy.kind', 'constant')
    pick('f-agents.1.policy.kind', 'constant')
    pick('f-budget.max_usd', '0.5')
    pick('f-batch_budget.max_calls', '1')
    fireEvent.click(screen.getByRole('button', { name: /Estimate cost/ }))
    await screen.findByText(/Maximum cost/)
    fireEvent.click(screen.getByRole('button', { name: /Start experiment/ }))
    const dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveTextContent('No model calls: every seller is scripted.')
    expect(dialog).not.toHaveTextContent(/paid|provider/i)
    expect(within(dialog).queryByRole('checkbox')).toBeNull()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Start' }))
    await waitFor(() => expect(router.state.location.pathname).toMatch(/^\/experiments\/exp_/))
  })

  it('an LLM agent only in one condition still makes the start paid, and is named with its condition', async () => {
    renderAt('/experiments/new')
    await chooseCells()
    pick('f-agents.0.policy.kind', 'constant')
    pick('f-agents.1.policy.kind', 'constant')
    fireEvent.click(screen.getByRole('checkbox', { name: 'Other agents for C2a' }))
    pick('f-agents_by_condition.C2a.0.policy.kind', 'llm')
    pick('f-budget.max_usd', '0.5')
    pick('f-batch_budget.max_usd', '100')
    fireEvent.click(screen.getByRole('button', { name: /Estimate cost/ }))
    await screen.findByText(/Maximum cost/)
    fireEvent.click(screen.getByRole('button', { name: /Start experiment/ }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByRole('list', { name: 'LLM agents' })).toHaveTextContent(/^C2a\/A: /)
    expect(within(dialog).getByRole('button', { name: 'Start paid runs' })).toBeDisabled()
  })

  it('a worst case above the batch budget keeps Start off', async () => {
    renderAt('/experiments/new')
    await chooseCells()
    pick('f-budget.max_usd', '0.5')
    pick('f-batch_budget.max_calls', '10')
    fireEvent.click(screen.getByRole('button', { name: /Estimate cost/ }))
    expect(await screen.findByText(/The worst case is above the batch max calls\./)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Start experiment/ })).toBeDisabled()
  })

  it('the experiment estimate shows the worst case of all runs against the batch caps (UX review item 1)', async () => {
    renderAt('/experiments/new')
    await chooseCells()
    pick('f-budget.max_usd', '0.5')
    pick('f-batch_budget.max_calls', '100000')
    fireEvent.click(screen.getByRole('button', { name: /Estimate cost/ }))
    const line = await screen.findByLabelText('Worst case against the caps')
    expect(line).toHaveTextContent(/^Worst case of all runs against the caps: [\d,]+ of 100,000 calls \(\d+%\)$/)
    expect(screen.getByText(/Maximum cost:/)).toHaveTextContent(/Maximum cost: \$\d+\.\d\d\.$/)
  })

  it('a batch cap below the worst case gets a button that sets that cap, estimates again and leaves the others alone (CR-87 minor 2)', async () => {
    renderAt('/experiments/new')
    await chooseCells()
    pick('f-budget.max_usd', '0.5')
    pick('f-batch_budget.max_calls', '10')
    pick('f-batch_budget.max_usd', '0.0001')
    fireEvent.click(screen.getByRole('button', { name: /Estimate cost/ }))
    await screen.findByText(/The worst case is above the batch max calls and the batch max USD\./)
    const raise = () => screen.queryAllByRole('button', { name: /^Raise / }).map((b) => b.textContent)
    expect(raise()).toHaveLength(2)
    expect(raise()[0]).toMatch(/^Raise batch max calls to [\d,]+ and estimate again$/)
    expect(raise()[1]).toMatch(/^Raise batch max USD to \$[\d.]+ and estimate again$/)
    expect(screen.getByText(/After a raise, a cap allows all runs: up to [\d,]+ calls, all runs: up to \$[\d.]+\./)).toBeInTheDocument()
    const before = bodies.filter((b) => b.url.endsWith('/api/experiments/estimate')).length
    fireEvent.click(screen.getByRole('button', { name: /^Raise batch max calls to/ }))
    await waitFor(() => expect(bodies.filter((b) => b.url.endsWith('/api/experiments/estimate'))).toHaveLength(before + 1))
    const sent = lastBody('/api/experiments/estimate').batch_budget
    expect(sent.max_calls).toBeGreaterThan(10)
    expect(sent.max_usd).toBe(0.0001)
    expect((document.getElementById('f-batch_budget.max_calls') as HTMLInputElement).value).toBe(String(sent.max_calls))
    await waitFor(() => expect(raise()).toHaveLength(1))
    fireEvent.click(screen.getByRole('button', { name: /^Raise batch max USD to/ }))
    await waitFor(() => expect(screen.getByRole('button', { name: /Start experiment/ })).toBeEnabled())
    expect(screen.queryAllByRole('button', { name: /^Raise / })).toHaveLength(0)
  })
})

describe('experiment list and detail', () => {
  it('lists experiments newest first with status and cell counts', async () => {
    await post('/experiments', body({ name: 'first' }))
    await post('/experiments', body({ name: 'second', conditions: ['C2a'] }))
    renderAt('/experiments')
    const rows = (await screen.findAllByRole('row')).slice(1)
    expect(rows.map((r) => within(r).getAllByRole('cell')[1]!.textContent)).toEqual([expect.stringMatching(/^second/), expect.stringMatching(/^first/)])
    expect(rows[1]).toHaveTextContent('4 queued')
  })

  it('the grid shows each cell: status, parent and child links; a blocked cell shows why', async () => {
    const x = (await post('/experiments', body())).json
    blockMockCell(x.id, 'C1', 2, 'hash_mismatch', 'Scenario scn_s2 changed since the freeze.')
    renderAt(`/experiments/${x.id}`)
    const grid = await screen.findByRole('table', { name: 'Cells' })
    const cell = (k: string) => grid.querySelector(`[data-cell="${k}"]`) as HTMLElement

    expect(within(cell('C1/1')).getByRole('link', { name: 'parent' })).toHaveAttribute('href', `/workbench/${x.cells[0]!.parent.run_id}`)
    expect(within(cell('C1/1')).getAllByRole('listitem').map((li) => li.textContent)).toEqual([
      'probe planned',
      'placebo planned',
      'placebo planned',
      'placebo planned',
      'ablation planned',
    ])
    expect(within(cell('C2a/1')).getAllByRole('listitem').map((li) => li.textContent)).toEqual(['probe planned', 'placebo planned', 'placebo planned', 'placebo planned', 'ablation planned'])

    expect(cell('C1/2')).toHaveTextContent('blocked')
    expect(within(cell('C1/2')).getByRole('alert')).toHaveTextContent(/hash_mismatch.*Scenario scn_s2 changed since the freeze/)
    expect(screen.getAllByRole('alert')[0]).toHaveTextContent('1 cell is blocked')
    expect(screen.getAllByRole('alert')[0]).not.toHaveTextContent(/restart the server/)
  })

  it('Retry blocked cells: a fixed cell moves on, a cell whose cause is still there is blocked again with a new time', async () => {
    const x = (await post('/experiments', body())).json
    blockMockCell(x.id, 'C1', 1, 'paid_calls_disabled', 'Paid calls are off.')
    blockMockCell(x.id, 'C1', 2, 'hash_mismatch', 'Scenario scn_s2 changed since the freeze.', { stuck: true })
    renderAt(`/experiments/${x.id}`)
    const grid = await screen.findByRole('table', { name: 'Cells' })
    const cell = (k: string) => grid.querySelector(`[data-cell="${k}"]`) as HTMLElement
    const callout = screen.getAllByRole('alert')[0]!
    expect(callout).toHaveTextContent('2 cells are blocked')
    const before = within(cell('C1/2')).getByRole('alert').textContent
    fireEvent.click(within(callout).getByRole('button', { name: 'Retry blocked cells' }))
    await waitFor(() => expect(cell('C1/1')).not.toHaveTextContent('blocked'))
    expect(cell('C1/2')).toHaveTextContent('blocked')
    expect(within(cell('C1/2')).getByRole('alert')).toHaveTextContent(/hash_mismatch/)
    expect(within(cell('C1/2')).getByRole('alert').textContent).not.toBe(before)
    expect(screen.getAllByRole('alert')[0]).toHaveTextContent('1 cell is blocked')
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('Retry on an experiment with LLM agents needs the paid approval; Cancel sends nothing', async () => {
    setMockPaidCalls(true)
    const llm = ['A', 'B'].map((id) => ({ id, role: 'seller', capability: null, policy: { kind: 'llm' as const, llm: { provider_profile_id: 'prof_mock', model: 'm' } } }))
    const x = (await post('/experiments', body({ agents: llm as never }))).json
    blockMockCell(x.id, 'C1', 1, 'paid_calls_disabled', 'Paid calls are off.')
    renderAt(`/experiments/${x.id}`)
    const grid = await screen.findByRole('table', { name: 'Cells' })
    const retries = () => bodies.filter((r) => r.url.endsWith('/retry')).length
    const open = () => fireEvent.click(within(screen.getAllByRole('alert')[0]!).getByRole('button', { name: 'Retry blocked cells' }))
    open()
    let dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByRole('list', { name: 'Blocked cells' })).toHaveTextContent('C1 seed 1')
    expect(dialog).toHaveTextContent(`up to ${x.estimate.calls_max.toLocaleString()} calls left`)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(retries()).toBe(0)
    open()
    dialog = await screen.findByRole('dialog')
    const go = within(dialog).getByRole('button', { name: 'Retry paid runs' })
    expect(go).toBeDisabled()
    fireEvent.click(within(dialog).getByRole('checkbox'))
    fireEvent.click(go)
    await waitFor(() => expect(grid.querySelector('[data-cell="C1/1"]')).not.toHaveTextContent('blocked'))
    expect(retries()).toBe(1)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('an experiment with no blocked cell shows no blocked note and no Retry', async () => {
    const x = (await post('/experiments', body())).json
    renderAt(`/experiments/${x.id}`)
    await screen.findByRole('table', { name: 'Cells' })
    expect(screen.queryByText(/blocked/)).toBeNull()
    expect(screen.queryByRole('button', { name: 'Retry blocked cells' })).toBeNull()
  })
})
