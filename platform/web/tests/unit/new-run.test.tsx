import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { http, HttpResponse } from 'msw/http'
import { setupServer } from 'msw/node'
import type { Preset, RunConfig } from '@/api/runConfig'
import { NewRunPage } from '@/features/new-run/NewRunPage'
import { handlers } from '@/mocks/handlers'
import { mockConditions, resetMockPresets, setMockResearchState } from '@/mocks/registry'

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
  server.resetHandlers()
  resetMockPresets()
})

function renderPage() {
  const router = createMemoryRouter(
    [
      { path: '/runs/new', element: <NewRunPage /> },
      { path: '/runs/:id', element: <p>run page</p> },
      { path: '/workbench/:runId', element: <p>workbench</p> },
      { path: '/runs', element: <p>runs page</p> },
    ],
    { initialEntries: ['/runs/new'] },
  )
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return router
}

const pick = (id: string, value: string) => fireEvent.change(document.getElementById(id)!, { target: { value } })
const lastBody = (path: string) => JSON.parse(bodies.filter((b) => b.url.endsWith(path)).at(-1)!.body)
/** The box of a read-only value: its label row and the value below it. */
const lockedBox = (label: string) => screen.getAllByText(label).map((el) => el.parentElement!.parentElement!)

/** Every field that the contract locks for research (x-research-locked in the real schema), read from a request body. */
function lockedValues(c: RunConfig) {
  return [
    c.scenario_id,
    c.scenario_hash,
    c.prompt_variant_id,
    c.channels,
    c.transparency,
    c.call_mode,
    c.observation_mode,
    c.limits,
    ...c.agents.flatMap((a) => [a.capability, a.policy.llm?.system_prompt ?? null, a.policy.llm?.talk_prompt ?? null]),
  ]
}

async function researchFor(condition: string) {
  fireEvent.click(await screen.findByRole('radio', { name: 'Research' }))
  await waitFor(() => expect(document.querySelector(`#f-condition_id option[value="${condition}"]`)).not.toBeNull())
  pick('f-condition_id', condition)
}

describe('new run form', () => {
  it('research: locked fields are shown read-only, nothing sets a label, and the request sends no locked field', async () => {
    const router = renderPage()
    await researchFor('C3-accept')
    expect(await screen.findByText(/Labels from the condition: legal yes, harm no/)).toBeInTheDocument()

    for (const id of ['f-scenario_id', 'f-prompt_variant_id', 'f-channels', 'f-transparency', 'f-call_mode', 'f-observation_mode', 'f-memory.summary', 'f-agents.0.policy.llm.system_prompt', 'f-agents.0.policy.llm.talk_prompt'])
      expect(document.getElementById(id)).toBeNull()
    expect(lockedBox('Call mode')[0]).toHaveTextContent('talk_and_act')
    expect(lockedBox('Observation mode')[0]).toHaveTextContent('push')
    expect(lockedBox('Scenario')[0]).toHaveTextContent('One scenario per seed, from the seed map')
    expect(document.getElementById('f-limits.max_steps_per_turn')).toBeNull()
    expect(lockedBox('Limits')[0]).toHaveTextContent(/max_steps_per_turn.*5/)
    expect(screen.queryByRole('button', { name: /Add agent/ })).toBeNull()
    const controls = [...document.querySelectorAll('input, select, textarea')]
    expect(controls.filter((el) => /label|legal|harm/i.test(`${el.id} ${el.getAttribute('name') ?? ''}`))).toEqual([])

    fireEvent.click(screen.getByRole('checkbox', { name: '1' }))
    fireEvent.click(screen.getByRole('checkbox', { name: '2' }))
    const variant = lockedBox('Prompt variant')[0]!
    expect(variant).toHaveTextContent('v1for seed 2')
    expect(variant).toHaveTextContent('v2for seed 1')
    expect(lockedBox('Talk prompt')[0]).toHaveTextContent('Propose that both of you keep your prices at a high level')

    pick('f-budget.max_usd', '0.5')
    fireEvent.click(screen.getByRole('button', { name: /Estimate cost/ }))
    expect(await screen.findByText(/Maximum cost/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Start 2 runs' }))
    expect(await screen.findByRole('dialog')).toHaveTextContent('LLM agents call their providers. Each run stops at its budget ($0.5).')
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Start' }))
    await waitFor(() => expect(router.state.location.pathname).toBe('/runs'))

    const start = lastBody('/api/runs') as RunConfig
    expect(start).toMatchObject({ kind: 'research', condition_id: 'C3-accept', split: 'pilot', seeds: [1, 2] })
    expect(start.memory?.summary ?? null).toBeNull()
    expect(lockedValues(start)).toEqual(lockedValues(start).map(() => null))
    expect(JSON.stringify(start)).not.toMatch(/"labels"|"legal"|"harm"/)
    // About 2 s alone; the whole form flow passes 5 s when all test files run in parallel.
  })

  it('research: a loaded preset that holds locked values still sends none of them', async () => {
    const old = {
      id: 'pre_old',
      name: 'Old research',
      version: 1,
      hash: 'sha256:x',
      created_at: '2026-10-21T12:00:00Z',
      config: {
        kind: 'research',
        condition_id: 'C1',
        split: 'pilot',
        seeds: [1],
        scenario_id: 'scn_s1',
        scenario_hash: 'sha256:s1',
        prompt_variant_id: 'v2',
        channels: { public: true, private_pairs: true, news: true },
        transparency: 'opaque',
        call_mode: 'one_call',
        clock: { mode: 'sync' },
        budget: { max_usd: 0.5 },
        memory: { summary: 'llm' },
        agents: ['A', 'B'].map((id) => ({
          id,
          role: 'seller',
          capability: { grants: [] },
          policy: { kind: 'llm' as const, llm: { provider_profile_id: 'prof_mock', model: 'mock-model', system_prompt: 'x', talk_prompt: 'y' } },
        })),
      },
    } satisfies Preset
    server.use(http.get('/api/presets', () => HttpResponse.json([old])))
    renderPage()
    await waitFor(() => expect(document.querySelector('#preset-select option[value="pre_old"]')).not.toBeNull())
    pick('preset-select', 'pre_old')
    fireEvent.click(screen.getByRole('button', { name: /Load/ }))
    fireEvent.click(await screen.findByRole('button', { name: /Estimate cost/ }))
    expect(await screen.findByText(/Maximum cost/)).toBeInTheDocument()
    const est = lastBody('/api/runs/estimate') as RunConfig
    expect(lockedValues(est)).toEqual(lockedValues(est).map(() => null))
    expect(est.memory?.summary ?? null).toBeNull()
  })

  it('research: dev is off for a draft condition, test is off until the freeze, and the seeds are the pilot seeds', async () => {
    renderPage()
    await researchFor('C1')
    const split = document.getElementById('f-split') as HTMLSelectElement
    const option = (v: string) => [...split.options].find((o) => o.value === v)!
    expect(option('dev')).toBeDisabled()
    expect(option('dev').textContent).toMatch(/C1 is a draft/)
    expect(option('test')).toBeDisabled()
    expect(screen.getByRole('checkbox', { name: '3' })).toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: '101' })).toBeNull()
  })

  it('CR-14 m2: changing from a frozen condition on dev to a draft condition moves the draft back to pilot with no seeds', async () => {
    setMockResearchState({ conditions: mockConditions.map((c) => (c.id === 'C2a' ? { ...c, status: 'frozen' as const, frozen_hash: c.hash } : c)) })
    renderPage()
    await researchFor('C2a')
    pick('f-split', 'dev')
    fireEvent.click(await screen.findByRole('checkbox', { name: '101' }))
    pick('f-condition_id', 'C1')
    const split = document.getElementById('f-split') as HTMLSelectElement
    expect(split.value).toBe('pilot')
    expect([...split.options].find((o) => o.value === 'dev')).toBeDisabled()
    expect(screen.getByRole('checkbox', { name: '1' })).not.toBeChecked()
    expect(screen.getByRole('button', { name: /Start 0 runs/ })).toBeDisabled()
  })

  it('CR-14 m1: a condition that changed after its freeze cannot be picked, and says why', async () => {
    setMockResearchState({ conditions: mockConditions.map((c) => (c.id === 'C1' ? { ...c, status: 'frozen' as const, frozen_hash: 'sha256:before-the-change' } : c)) })
    renderPage()
    fireEvent.click(await screen.findByRole('radio', { name: 'Research' }))
    await waitFor(() => expect(document.querySelector('#f-condition_id option[value="C1"]')).not.toBeNull())
    const c1 = document.querySelector<HTMLOptionElement>('#f-condition_id option[value="C1"]')!
    expect(c1).toBeDisabled()
    expect(c1.textContent).toMatch(/changed since the freeze; the server refuses it/)
    expect(document.querySelector('#f-condition_id option[value="C2a"]')).not.toBeDisabled()
  })

  it('research: shows the server error when the seed map or a condition file is broken', async () => {
    setMockResearchState({ seeds: 'missing', invalidCondition: 'C2b' })
    renderPage()
    fireEvent.click(await screen.findByRole('radio', { name: 'Research' }))
    expect(await screen.findByText(/conditions\/seeds.yaml does not exist/)).toBeInTheDocument()
    expect(await screen.findByText(/Condition file C2b.yaml is invalid/)).toBeInTheDocument()
  })

  it('shows server field errors next to the field and keeps Start off until an estimate passes', async () => {
    renderPage()
    fireEvent.click(await screen.findByRole('radio', { name: 'Research' }))
    expect(screen.getByRole('button', { name: /Start 0 runs/ })).toBeDisabled()
    pick('f-budget.max_calls', '10')
    fireEvent.click(screen.getByRole('button', { name: /Estimate cost/ }))
    expect(await screen.findByText('A research run needs a condition_id.')).toBeInTheDocument()
    expect(document.getElementById('f-condition_id')!.parentElement).toHaveTextContent('required')
    expect(screen.getByRole('button', { name: /Start 0 runs/ })).toBeDisabled()
  })

  it('the start dialog says when every agent is scripted, and names no provider calls', async () => {
    renderPage()
    await screen.findByRole('button', { name: 'Add agent' })
    pick('f-agents.0.policy.kind', 'constant')
    pick('f-agents.1.policy.kind', 'constant')
    pick('f-budget.max_calls', '10')
    fireEvent.click(screen.getByRole('button', { name: /Estimate cost/ }))
    await screen.findByText(/Maximum cost/)
    expect(lastBody('/api/runs/estimate').agents.map((a: { policy: { kind: string } }) => a.policy.kind)).toEqual(['constant', 'constant'])
    fireEvent.click(screen.getByRole('button', { name: 'Start 1 run' }))
    const dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveTextContent('Every agent is scripted, so the runs make no model calls.')
    expect(dialog).not.toHaveTextContent('call their providers')
  })

  it('exploratory: a granted tool and its grant params reach the request; an ungranted tool does not', async () => {
    renderPage()
    await screen.findByRole('button', { name: 'Add agent' })
    fireEvent.change(document.getElementById('f-agents.0.capability.grants.0.params.depth')!, { target: { value: '3' } })
    const postRow = screen.getAllByText('post', { selector: '.font-mono' })[0]!.closest('li')!
    fireEvent.click(within(postRow).getByRole('checkbox'))
    pick('f-budget.max_usd', '0.5')
    pick('f-memory.full_rounds', '7')
    fireEvent.click(screen.getByRole('button', { name: /Estimate cost/ }))
    await screen.findByText(/Maximum cost/)
    const est = lastBody('/api/runs/estimate')
    expect(est.memory).toEqual({ full_rounds: 7 })
    const grants = est.agents[0].capability.grants as { tool_id: string; params: Record<string, unknown> }[]
    expect(grants.find((g) => g.tool_id === 'get_rival_prices')!.params).toEqual({ depth: 3 })
    expect(grants.map((g) => g.tool_id)).not.toContain('post')
    expect(est).toMatchObject({ condition_id: null, prompt_variant_id: null, split: 'exploratory' })
    expect(est.labels).toBeUndefined()
  })

  it('a run switch shows its grant as off, keeps it in the request, and gives it back when the switch is on (CR-69)', async () => {
    renderPage()
    await screen.findByRole('button', { name: 'Add agent' })
    const row = (tool: string) => screen.getAllByText(tool, { selector: '.font-mono' })[0]!.closest('li')!
    const box = (tool: string) => within(row(tool)).getByRole('checkbox') as HTMLInputElement
    expect([box('get_news').checked, box('get_rival_prices').checked]).toEqual([true, true])
    const summary = () => screen.getAllByText(/^Tools \(\d+ of \d+ granted\)$/)[0]!.textContent
    const before = summary()
    // An enum select has the option index as its value.
    const pickText = (id: string, text: string) => {
      const el = document.getElementById(id) as HTMLSelectElement
      pick(id, [...el.options].find((o) => (o.dataset.raw ?? o.text).includes(text))!.value)
    }
    fireEvent.click(document.getElementById('f-channels.news')!)
    expect([box('get_news').checked, box('get_news').disabled]).toEqual([false, true])
    expect(row('get_news')).toHaveTextContent('Off: the run has no news (Market rules, channels).')
    pickText('f-transparency', 'opaque')
    expect([box('get_rival_prices').checked, box('get_rival_prices').disabled]).toEqual([false, true])
    expect(row('get_rival_prices')).toHaveTextContent('Off: the run is opaque (Market rules, transparency).')
    expect(document.getElementById('f-agents.0.capability.grants.0.params.depth')).toBeNull()
    expect(summary()).not.toBe(before)
    pick('f-budget.max_usd', '0.5')
    fireEvent.click(screen.getByRole('button', { name: /Estimate cost/ }))
    await screen.findByText(/Maximum cost/)
    const est = lastBody('/api/runs/estimate')
    expect(est).toMatchObject({ channels: { news: false }, transparency: 'opaque' })
    expect((est.agents[0].capability.grants as { tool_id: string }[]).map((g) => g.tool_id)).toEqual(expect.arrayContaining(['get_news', 'get_rival_prices']))
    fireEvent.click(document.getElementById('f-channels.news')!)
    pickText('f-transparency', 'rival_last_round')
    expect([box('get_news').checked, box('get_rival_prices').checked, box('get_news').disabled]).toEqual([true, true, false])
    expect(summary()).toBe(before)
  })

  it('CR-44 1: a 501 presets route shows one plain sentence, no red error, no preset buttons, and is not asked again', async () => {
    let calls = 0
    server.use(
      http.get('/api/presets', () => {
        calls++
        return HttpResponse.json({ code: 'not_built', message: 'GET /api/presets is in the contract but not built yet' }, { status: 501 })
      }),
    )
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const mount = () =>
      render(
        <QueryClientProvider client={client}>
          <RouterProvider router={createMemoryRouter([{ path: '/runs/new', element: <NewRunPage /> }], { initialEntries: ['/runs/new'] })} />
        </QueryClientProvider>,
      )
    mount()
    await screen.findByText(/^Presets are not available on this server\./)
    expect(screen.queryByText(/not built yet/)).toBeNull()
    expect(screen.queryByRole('button', { name: /Save as preset/ })).toBeNull()
    cleanup()
    mount()
    await screen.findByText(/^Presets are not available on this server\./)
    expect(calls).toBe(1)
  })

  it('CR-44 2: the Policy options are labelled by id, and the description shows under the field', async () => {
    renderPage()
    await screen.findByRole('button', { name: 'Add agent' })
    const select = document.getElementById('f-agents.0.policy.kind') as HTMLSelectElement
    expect([...select.options].map((o) => o.textContent)).toEqual([...select.options].map((o) => o.value))
    pick('f-agents.0.policy.kind', 'scripted_talker')
    expect(select.parentElement).toHaveTextContent('Talks, never changes price')
  })
})

describe('Agents editor switches (0.30.0: observation mode, new tools, channel switches)', () => {
  const row = (tool: string) => screen.getAllByText(tool, { selector: '.font-mono' })[0]!.closest('li')!
  const box = (tool: string) => within(row(tool)).getByRole('checkbox') as HTMLInputElement
  // An enum select has the option index as its value.
  const pickText = (id: string, text: string) => {
    const el = document.getElementById(id) as HTMLSelectElement
    pick(id, [...el.options].find((o) => (o.dataset.raw ?? o.text) === text)!.value)
  }
  const estimate = async () => {
    pick('f-budget.max_usd', '0.5')
    fireEvent.click(screen.getByRole('button', { name: /Estimate cost/ }))
    await screen.findByText(/Maximum cost/)
    return lastBody('/api/runs/estimate') as RunConfig
  }

  it('a tool row says "log only (layer A)" with a tooltip that explains the layer (UX review item 10)', async () => {
    renderPage()
    await screen.findByRole('button', { name: 'Add agent' })
    const note = within(row('get_news')).getByText(/log only \(layer A\)/)
    expect(note).toHaveAttribute('title', 'Layer A: a feature that reads this tool needs only the log.')
  })

  it('the new tools are offered but not granted; read_channel and post name public and the pair', async () => {
    renderPage()
    await screen.findByRole('button', { name: 'Add agent' })
    for (const id of ['calc_profit', 'get_price_history', 'write_note', 'read_note']) expect([id, box(id).checked, box(id).disabled]).toEqual([id, false, false])
    fireEvent.click(box('calc_profit'))
    const est = await estimate()
    const grants = est.agents[0]!.capability!.grants
    expect(grants.map((g) => g.tool_id)).toContain('calc_profit')
    expect(grants.map((g) => g.tool_id)).not.toContain('write_note')
    expect(grants.find((g) => g.tool_id === 'post')!.params).toMatchObject({ channels: ['public', 'private_A_B'] })
    expect(est.agents[1]!.capability!.grants.find((g) => g.tool_id === 'read_channel')!.params).toEqual({ channels: ['public', 'private_A_B'] })
  })

  it('observation mode push keeps the pushed items on and editable with a note; only get_price_history is off (CR-83 1)', async () => {
    renderPage()
    await screen.findByRole('button', { name: 'Add agent' })
    fireEvent.click(box('get_price_history'))
    expect(screen.getByRole('heading', { name: 'Market rules' }).closest('section')).toContainElement(document.getElementById('f-observation_mode'))
    pickText('f-observation_mode', 'push')
    for (const id of ['get_rival_prices', 'get_news', 'read_channel', 'get_own_sales']) {
      expect([id, box(id).checked, box(id).disabled]).toEqual([id, true, false])
      expect(row(id)).toHaveTextContent('Push shows this item in every call. The agent has no read tool for it.')
      expect(row(id)).not.toHaveTextContent('Off:')
    }
    expect([box('get_price_history').checked, box('get_price_history').disabled]).toEqual([false, true])
    expect(row('get_price_history')).toHaveTextContent('Off: observation mode push gives no read, and this tool has no pushed field (Market rules).')
    expect([box('post').checked, box('calc_profit').disabled]).toEqual([true, false])
    // The user can still take a pushed item out of an agent: no news for it.
    fireEvent.click(box('get_news'))
    expect(box('get_news').checked).toBe(false)
    const est = await estimate()
    expect(est.observation_mode).toBe('push')
    const ids = est.agents[0]!.capability!.grants.map((g) => g.tool_id)
    expect(ids).toEqual(expect.arrayContaining(['get_rival_prices', 'read_channel', 'get_price_history']))
    expect(ids).not.toContain('get_news')
    pickText('f-observation_mode', 'both')
    expect([box('get_rival_prices').checked, box('get_price_history').checked, box('get_price_history').disabled]).toEqual([true, true, false])
    expect(row('get_rival_prices')).not.toHaveTextContent('Push shows')
  })

  it('observation mode pull with no read tool granted says that the agent sees nothing, and the run is not refused (CR-83 4)', async () => {
    renderPage()
    await screen.findByRole('button', { name: 'Add agent' })
    pickText('f-observation_mode', 'pull')
    expect(screen.queryByText(/no read tool is granted/)).toBeNull()
    for (const id of ['get_rival_prices', 'get_news', 'read_channel', 'get_own_sales']) fireEvent.click(box(id))
    expect((await screen.findAllByText(/Observation mode pull gives A only the round and its last result, and no read tool is granted/)).length).toBeGreaterThan(0)
    fireEvent.click(box('get_own_sales'))
    await waitFor(() => expect(screen.queryAllByText(/Observation mode pull gives A only/)).toHaveLength(0))
  })

  it('observation mode pull with no read calls says that the server refuses the run', async () => {
    renderPage()
    await screen.findByRole('button', { name: 'Add agent' })
    pickText('f-observation_mode', 'pull')
    expect(screen.queryByText(/Observation mode pull needs read calls/)).toBeNull()
    pick('f-agents.0.capability.max_queries_per_round', '0')
    expect(await screen.findByText('Observation mode pull needs read calls, but A has none (limit 3, tool grant 0). The server refuses this run.')).toBeInTheDocument()
    expect(screen.getAllByText(/Observation mode pull needs read calls/)).toHaveLength(1)
  })

  it('a closed channel: a note when the grant keeps another channel, off when it keeps none (CR-69 addendum 2)', async () => {
    renderPage()
    await screen.findByRole('button', { name: 'Add agent' })
    fireEvent.click(document.getElementById('f-channels.public')!)
    for (const id of ['read_channel', 'post']) {
      expect([id, box(id).checked, box(id).disabled]).toEqual([id, true, false])
      expect(row(id)).toHaveTextContent('The run does not open public, so the server takes it out of this grant.')
    }
    fireEvent.click(document.getElementById('f-channels.private_pairs')!)
    for (const id of ['read_channel', 'post']) {
      expect([id, box(id).checked, box(id).disabled]).toEqual([id, false, true])
      expect(row(id)).toHaveTextContent('Off: the run does not open public, private_A_B (Market rules, channels).')
    }
    const est = await estimate()
    expect(est.channels).toMatchObject({ public: false, private_pairs: false })
    expect(est.agents[0]!.capability!.grants.find((g) => g.tool_id === 'post')!.params).toMatchObject({ channels: ['public', 'private_A_B'] })
  })

  it('adding a seller gives it public and both pairs, and the others get its pair; removing it takes the pair out', async () => {
    renderPage()
    await screen.findByRole('button', { name: 'Add agent' })
    fireEvent.click(screen.getByRole('button', { name: 'Add agent' }))
    await screen.findByRole('button', { name: 'Remove agent C' })
    const channelsOf = (est: RunConfig) => est.agents.filter((a) => a.role === 'seller').map((a) => a.capability!.grants.find((g) => g.tool_id === 'post')!.params!.channels)
    expect(channelsOf(await estimate())).toEqual([
      ['public', 'private_A_B', 'private_A_C'],
      ['public', 'private_A_B', 'private_B_C'],
      ['public', 'private_A_C', 'private_B_C'],
    ])
    fireEvent.click(screen.getByRole('button', { name: 'Remove agent C' }))
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Remove agent C' })).toBeNull())
    expect(channelsOf(await estimate())).toEqual([
      ['public', 'private_A_B'],
      ['public', 'private_A_B'],
    ])
  })
})

describe('Start rule: raise a cap in one click (FE-realbackend finding 2)', () => {
  const fill = (id: string, v: string) => pick(id, v)
  const estimate = async () => {
    fireEvent.click(screen.getByRole('button', { name: /Estimate cost/ }))
    await screen.findByText(/The worst case (of a run )?is above the/)
  }
  const start = () => screen.getByRole('button', { name: /^Start 1 run/ }) as HTMLButtonElement
  const raiseButtons = () => screen.queryAllByRole('button', { name: /^Raise / }).map((b) => b.textContent)

  it('a cap below the worst case: Start is off, each cap has a button, and the page says why a run makes fewer calls', async () => {
    renderPage()
    await screen.findByRole('button', { name: 'Add agent' })
    fill('f-budget.max_calls', '100')
    fill('f-budget.max_tokens', '1000')
    fill('f-budget.max_usd', '0.01')
    await estimate()
    expect(start().disabled).toBe(true)
    expect(raiseButtons()).toEqual(['Raise max calls to 312 and estimate again', 'Raise max tokens to 686,400 and estimate again', 'Raise max USD to $0.14 and estimate again'])
    expect(screen.getByText(/so a run makes far fewer calls\. A cap below the worst case can still stop a run early \(end reason budget_exceeded\)\. A cap at the worst case never does\./)).toBeInTheDocument()
  })

  it('one click sets that cap, estimates again, and leaves the other caps alone; the last click enables Start', async () => {
    renderPage()
    await screen.findByRole('button', { name: 'Add agent' })
    fill('f-budget.max_calls', '100')
    fill('f-budget.max_tokens', '1000')
    await estimate()
    const before = bodies.filter((b) => b.url.endsWith('/api/runs/estimate')).length
    fireEvent.click(screen.getByRole('button', { name: /^Raise max calls to 312/ }))
    await waitFor(() => expect(bodies.filter((b) => b.url.endsWith('/api/runs/estimate'))).toHaveLength(before + 1))
    expect(lastBody('/api/runs/estimate').budget).toMatchObject({ max_calls: 312, max_tokens: 1000 })
    expect((document.getElementById('f-budget.max_calls') as HTMLInputElement).value).toBe('312')
    await waitFor(() => expect(raiseButtons()).toEqual(['Raise max tokens to 686,400 and estimate again']))
    expect(start().disabled).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: /^Raise max tokens to 686,400/ }))
    await waitFor(() => expect(start().disabled).toBe(false))
    expect(screen.queryByText(/The worst case of a run is above the budget/)).toBeNull()
    expect(screen.getByText(/Maximum cost/)).toBeInTheDocument()
    expect(lastBody('/api/runs/estimate').budget).toMatchObject({ max_calls: 312, max_tokens: 686400 })
  })

  it('the page says what a raised cap allows, and which cap is below the worst case (CR-87 minors 1 and 2)', async () => {
    renderPage()
    await screen.findByRole('button', { name: 'Add agent' })
    fill('f-budget.max_calls', '100')
    fill('f-budget.max_usd', '0.01')
    await estimate()
    expect(screen.getByText(/The worst case is above the max calls and the max USD\. Raise a cap, or reduce rounds or agents\./)).toBeInTheDocument()
    expect(screen.getByText(/After a raise, a cap allows each run: up to 312 calls, each run: up to \$0\.14\./)).toBeInTheDocument()
  })

  it('Start anyway: shown only above the worst case, opens the dialog, warns that a run can stop early, and starts', async () => {
    const router = renderPage()
    await screen.findByRole('button', { name: 'Add agent' })
    expect(screen.queryByRole('button', { name: 'Start anyway' })).toBeNull()
    fill('f-budget.max_calls', '100')
    await estimate()
    expect(start().disabled).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Start anyway' }))
    const dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveTextContent('The worst case is above a cap, so a run can stop early (end reason budget_exceeded).')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Start' }))
    await waitFor(() => expect(router.state.location.pathname).toMatch(/^\/workbench\/run_/))
    expect(lastBody('/api/runs').budget).toMatchObject({ max_calls: 100 })
  })

  it('the estimate shows the worst case against each cap, and money with two decimals (UX review item 1)', async () => {
    renderPage()
    await screen.findByRole('button', { name: 'Add agent' })
    fill('f-budget.max_calls', '400')
    fill('f-budget.max_usd', '0.14')
    fireEvent.click(screen.getByRole('button', { name: /Estimate cost/ }))
    const line = await screen.findByLabelText('Worst case against the caps')
    expect(line).toHaveTextContent('Worst case of one run against the caps: $0.13 of $0.14 (94%) · 312 of 400 calls (78%)')
    expect(screen.getByText(/Maximum cost:/)).toHaveTextContent('Maximum cost: $0.13.')
    expect(screen.queryByText(/\$0\.1310/)).toBeNull()
  })

  it('a cap that clears the worst case shows no raise button and no warning', async () => {
    renderPage()
    await screen.findByRole('button', { name: 'Add agent' })
    fill('f-budget.max_calls', '5000')
    fireEvent.click(screen.getByRole('button', { name: /Estimate cost/ }))
    await screen.findByText(/Maximum cost/)
    expect(raiseButtons()).toEqual([])
    expect(screen.queryByText(/above the budget/)).toBeNull()
    expect(start().disabled).toBe(false)
    expect(screen.queryByRole('button', { name: 'Start anyway' })).toBeNull()
  })
})

describe('the thinking setting (0.32.0)', () => {
  it('is a select with true, false and the efforts, and each choice reaches the request with its type', async () => {
    renderPage()
    await screen.findByRole('button', { name: 'Add agent' })
    const el = document.getElementById('f-agents.0.policy.llm.thinking') as HTMLSelectElement
    expect(el.tagName).toBe('SELECT')
    expect([...el.options].map((o) => o.text)).toEqual(['Default (not set)', 'true', 'false', 'minimal', 'low', 'medium', 'high', 'xhigh'])
    pick('f-budget.max_calls', '5000')
    const thinking = async (text: string) => {
      pick('f-agents.0.policy.llm.thinking', [...el.options].find((o) => (o.dataset.raw ?? o.text) === text)!.value)
      fireEvent.click(screen.getByRole('button', { name: /Estimate cost/ }))
      await waitFor(() => expect(lastBody('/api/runs/estimate').agents[0].policy.llm.thinking).toBe(text === 'true' ? true : text === 'false' ? false : text))
    }
    await thinking('true')
    await thinking('false')
    await thinking('low')
  })
})

describe('the null item of an enum (FE-realbackend finding 3)', () => {
  it('the memory Summary list has a null item that is not "None", next to the value "none"', async () => {
    renderPage()
    await screen.findByRole('button', { name: 'Add agent' })
    const el = document.getElementById('f-memory.summary') as HTMLSelectElement
    expect([...el.options].map((o) => o.dataset.raw ?? o.text)).toEqual(['Default (not set)', 'code', 'llm', 'none'])
    expect([...el.options].map((o) => o.text)).toEqual(['Default (not set)', 'Notes written by code', 'Notes written by the model', 'No notes'])
    expect(new Set([...el.options].map((o) => o.text.toLowerCase())).size).toBe(4)
    pick('f-memory.summary', [...el.options].find((o) => o.dataset.raw === 'none')!.value)
    pick('f-budget.max_calls', '5000')
    fireEvent.click(screen.getByRole('button', { name: /Estimate cost/ }))
    await screen.findByText(/Maximum cost/)
    expect(lastBody('/api/runs/estimate').memory.summary).toBe('none')
  })

  it('every nullable enum uses the same label', async () => {
    renderPage()
    await screen.findByRole('button', { name: 'Add agent' })
    for (const id of ['f-transparency', 'f-call_mode', 'f-observation_mode', 'f-memory.summary']) {
      expect([...(document.getElementById(id) as HTMLSelectElement).options][0]!.text, id).toBe('Default (not set)')
    }
  })
})

describe('the setup page (UX review item 10)', () => {
  it('a section index links to each section', async () => {
    renderPage()
    await screen.findByRole('button', { name: 'Add agent' })
    const nav = screen.getByRole('navigation', { name: 'Sections' })
    for (const a of within(nav).getAllByRole('link')) {
      const id = a.getAttribute('href')!.slice(1)
      expect(document.getElementById(id), id).not.toBeNull()
    }
    expect(within(nav).getByRole('link', { name: 'Agents' })).toBeInTheDocument()
  })

  it('a changed draft asks the browser before a reload, and an untouched one does not', async () => {
    renderPage()
    await screen.findByRole('button', { name: 'Add agent' })
    const fire = () => {
      const e = new Event('beforeunload', { cancelable: true })
      window.dispatchEvent(e)
      return e.defaultPrevented
    }
    expect(fire()).toBe(false)
    expect(screen.queryByText(/This setup is not saved/)).toBeNull()
    pick('f-budget.max_calls', '4321')
    await screen.findByText(/This setup is not saved/)
    expect(fire()).toBe(true)
  })

  it('the preset note says why there is no preset bar', async () => {
    server.use(http.get('*/api/presets', () => HttpResponse.json({ code: 'not_built', message: 'not built' }, { status: 501 })))
    renderPage()
    expect(await screen.findByText(/This is normal: a preset is a saved setup/)).toBeInTheDocument()
  })
})

describe('the budget with no cap and the null placeholders (UX review 2, items 5 and 6)', () => {
  it('an empty budget says so in plain words, and the note goes when a cap is set', async () => {
    renderPage()
    await screen.findByRole('button', { name: 'Add agent' })
    const budget = document.getElementById('f-budget.max_usd')!.closest('fieldset')!
    expect(within(budget).getByRole('note')).toHaveTextContent('No per-run cap set. Add Max calls, Max tokens, or Max USD before estimating.')
    expect(screen.getByRole('button', { name: /Estimate cost/ })).toBeDisabled()
    pick('f-budget.max_calls', '5000')
    expect(within(budget).getAllByRole('note')).toHaveLength(1)
    expect(within(budget).getByRole('note')).toHaveTextContent(/^Per-run caps set: 5,000 calls\./)
    expect(within(budget).getByRole('note')).not.toHaveTextContent('No cap')
    expect(screen.getByRole('button', { name: /Estimate cost/ })).toBeEnabled()
  })

  it('the validation wording of the server is not shown: the page says what to do', async () => {
    server.use(
      http.post('/api/runs/estimate', () =>
        HttpResponse.json(
          { title: 'x', status: 422, code: 'validation_error', message: 'Value error, set at least one of max_usd, max_calls and max_tokens', errors: [{ path: 'budget', message: 'Value error, set at least one of max_usd, max_calls and max_tokens' }] },
          { status: 422, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    renderPage()
    await screen.findByRole('button', { name: 'Add agent' })
    pick('f-budget.max_calls', '5000')
    fireEvent.click(screen.getByRole('button', { name: /Estimate cost/ }))
    const alerts = await screen.findAllByText(/No per-run cap set. Add Max calls/, { selector: '[role=alert]' })
    expect(alerts.length).toBeGreaterThan(0)
    for (const a of screen.getAllByRole('alert')) expect(a).not.toHaveTextContent(/Value error|422|validation_error/)
    expect(alerts).toHaveLength(1)
  })

  it('an empty nullable number field has no placeholder "null"', async () => {
    renderPage()
    await screen.findByRole('button', { name: 'Add agent' })
    for (const id of ['f-budget.max_usd', 'f-budget.max_calls', 'f-budget.max_tokens']) expect(document.getElementById(id)!.getAttribute('placeholder')).not.toBe('null')
    const holders = [...document.querySelectorAll('input[type=number]')].map((el) => el.getAttribute('placeholder'))
    expect(holders).not.toContain('null')
  })
})
