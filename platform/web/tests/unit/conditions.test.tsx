import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { cleanup, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { setupServer } from 'msw/node'
import type { RunConfig } from '@/api/runConfig'
import { ConditionPage, ConditionsPage } from '@/features/conditions/ConditionsPage'
import { handlers } from '@/mocks/handlers'
import { checkRunConfig, mockConditions, resetMockPresets, setMockResearchState } from '@/mocks/registry'
import type { Condition } from '@/api/runConfig'

const frozen = (c: Condition): Condition => ({ ...c, status: 'frozen', frozen_hash: c.hash })
const changed = (c: Condition): Condition => ({ ...c, status: 'frozen', frozen_hash: 'sha256:before-the-change' })

const server = setupServer(...handlers)
beforeAll(() => server.listen({ onUnhandledFrame: 'bypass' }))
afterAll(() => server.close())
afterEach(() => {
  cleanup()
  resetMockPresets()
})

function renderAt(path: string) {
  const router = createMemoryRouter(
    [
      { path: '/conditions', element: <ConditionsPage /> },
      { path: '/conditions/:conditionId', element: <ConditionPage /> },
    ],
    { initialEntries: [path] },
  )
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

const noControls = () => expect(document.querySelectorAll('input, select, textarea, button')).toHaveLength(0)

describe('conditions pages', () => {
  it('lists the five conditions with their labels and status, plus the seeds and the freeze state, with nothing to edit', async () => {
    renderAt('/conditions')
    const rows = (await screen.findAllByRole('row')).slice(1)
    expect(rows.map((r) => within(r).getAllByRole('cell').slice(0, 5).map((c) => c.textContent))).toEqual([
      ['C1Coordinated harmful action', 'C1', 'yes', 'yes', 'draft'],
      [expect.stringMatching(/^C2a/), 'C2a', 'no', 'no', 'draft'],
      [expect.stringMatching(/^C2b/), 'C2b', 'no', 'no', 'draft'],
      ['C3-acceptCollusive-sounding talk, offer accepted', 'C3', 'yes', 'no', 'draft'],
      [expect.stringMatching(/^C3-silent/), 'C3', 'no', 'no', 'draft'],
    ])
    expect(await screen.findByText('locked until the freeze')).toBeInTheDocument()
    expect(screen.getByText('Freeze: not frozen')).toBeInTheDocument()
    expect(screen.getByRole('list', { name: 'Freeze problems' })).toHaveTextContent('frozen.json not found')
    noControls()
  })

  it('shows one condition read-only: settings, tools per seller, and each prompt variant with its seeds', async () => {
    renderAt('/conditions/C3-accept')
    expect(await screen.findByRole('heading', { name: /C3-accept: Collusive-sounding talk, offer accepted/ })).toBeInTheDocument()
    expect(screen.getByText('Labels').nextSibling).toHaveTextContent('legal yes, harm no')
    expect(screen.getByText('Talk kept from pricing').nextSibling).toHaveTextContent('yes')
    expect(screen.getByText('Observation').nextSibling).toHaveTextContent('push')
    expect(screen.getByText('Frozen hash').nextSibling).toHaveTextContent('none (draft)')
    expect(screen.getAllByText('Seller A')[0]!.parentElement).toHaveTextContent('read_channel {"channels":["private_A_B"]}')
    expect(screen.getByText('v1').parentElement).toHaveTextContent('(even seeds)')
    expect(screen.getByText('v2').parentElement).toHaveTextContent('(odd seeds)')
    expect(screen.getAllByText(/Propose that both of you keep your prices at a high level/)).toHaveLength(1)
    noControls()
  })

  it('CR-14 m1: a condition that changed after its freeze is marked in the list and explained on its page; freeze problems are listed', async () => {
    setMockResearchState({ conditions: mockConditions.map((c) => (c.id === 'C1' ? changed(c) : c)) })
    renderAt('/conditions')
    const c1 = (await screen.findByRole('link', { name: 'C1' })).closest('tr')!
    expect(c1).toHaveTextContent('frozenchanged since the freeze')
    expect(screen.getByRole('link', { name: 'C2a' }).closest('tr')).not.toHaveTextContent('changed since the freeze')
    expect(within(await screen.findByRole('list', { name: 'Freeze problems' })).getAllByRole('listitem').map((li) => li.textContent)).toEqual(['frozen.json not found'])
    cleanup()
    renderAt('/conditions/C1')
    expect(await screen.findByRole('alert')).toHaveTextContent('The server refuses every research run on it (409 hash_mismatch)')
  })
})

describe('mock research checks follow the backend order and codes', () => {
  const c1 = mockConditions.find((c) => c.id === 'C1')!
  const base: RunConfig = {
    kind: 'research',
    condition_id: 'C1',
    split: 'pilot',
    seeds: [1],
    clock: { mode: 'sync' },
    budget: { max_usd: 0.5 },
    agents: Object.keys(c1.capabilities).map((id) => ({ id, role: 'seller', policy: { kind: 'constant', params: { price: 5 } }, capability: null })),
  }
  const check = (c: Partial<RunConfig>) => {
    const bad = checkRunConfig({ ...base, ...c })
    return bad && { status: bad.status ?? 422, code: bad.code, paths: bad.errors.map((e) => e.path) }
  }

  it('accepts a clean research config', () => {
    expect(check({})).toBeNull()
  })

  it('names every locked field that a request sets, with slash paths', () => {
    expect(check({ channels: { public: true }, agents: base.agents.map((a, i) => (i ? a : { ...a, capability: { grants: [] } })) })).toEqual({
      status: 422,
      code: 'research_field_locked',
      paths: ['channels', 'agents/0/capability'],
    })
    expect(check({ memory: { summary: 'llm' } })).toEqual({ status: 422, code: 'research_field_locked', paths: ['memory/summary'] })
  })

  it('refuses test seeds, dev seeds on a draft condition, and seeds of another split', () => {
    expect(check({ split: 'test', seeds: [201] })).toMatchObject({ status: 409, code: 'test_split_locked' })
    expect(check({ split: 'dev', seeds: [101] })).toMatchObject({ status: 409, code: 'condition_not_frozen' })
    expect(check({ seeds: [101] })).toEqual({ status: 422, code: 'seed_not_in_split', paths: ['seeds'] })
  })

  it('refuses an exploratory config that names a condition or a prompt variant', () => {
    expect(check({ kind: 'exploratory', split: 'exploratory', prompt_variant_id: 'v1', condition_id: null })).toMatchObject({ status: 422, code: 'validation_error' })
  })

  it('CR-14 m3: a condition that changed after its freeze is refused on every split, before the split checks', () => {
    setMockResearchState({ conditions: mockConditions.map((c) => (c.id === 'C1' ? changed(c) : c)) })
    expect(check({})).toMatchObject({ status: 409, code: 'hash_mismatch' })
    expect(check({ split: 'test', seeds: [201] })).toMatchObject({ status: 409, code: 'hash_mismatch' })
    setMockResearchState({ conditions: mockConditions.map((c) => (c.id === 'C1' ? frozen(c) : c)) })
    expect(check({ split: 'dev', seeds: [101] })).toBeNull()
  })

  it('CR-14 m3: the general checks give slash paths, as the backend does', () => {
    const twoA = base.agents.map((a) => ({ ...a, id: 'A' }))
    expect(check({ kind: 'exploratory', split: 'exploratory', condition_id: null, scenario_id: null, budget: {}, agents: twoA })?.paths).toEqual(['scenario_id', 'agents/1/id', 'budget/max_usd'])
  })
})
