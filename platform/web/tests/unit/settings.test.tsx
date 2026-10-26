import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryRouter, Navigate, RouterProvider } from 'react-router'
import { setupServer } from 'msw/node'
import { ProvidersPage } from '@/features/providers/ProvidersPage'
import { ConditionFilePage, ConditionsSettingsPage } from '@/features/settings/ConditionsSettingsPage'
import { DefaultsPage } from '@/features/settings/DefaultsPage'
import { CONFIRM_WORD, FreezePage } from '@/features/settings/FreezePage'
import { ResearchFilesPage } from '@/features/settings/ResearchFilesPage'
import { SettingsLayout } from '@/features/settings/SettingsLayout'
import { handlers } from '@/mocks/handlers'
import { resetMockProfiles } from '@/mocks/providers'
import { resetMockPresets, setMockDefaults } from '@/mocks/registry'
import { resetMockSettings } from '@/mocks/settings'

const server = setupServer(...handlers)
const sent: { method: string; path: string; body: unknown }[] = []
server.events.on('request:start', async ({ request }) => {
  if (request.method !== 'GET') sent.push({ method: request.method, path: new URL(request.url).pathname, body: await request.clone().json().catch(() => null) })
})
beforeAll(() => server.listen({ onUnhandledFrame: 'bypass' }))
afterAll(() => server.close())
afterEach(() => {
  cleanup()
  server.resetHandlers()
  resetMockPresets()
  resetMockProfiles()
  resetMockSettings()
  sent.length = 0
})

function renderAt(path: string) {
  const router = createMemoryRouter(
    [
      { path: '/providers', element: <Navigate to="/settings/profiles" replace /> },
      {
        path: '/settings',
        element: <SettingsLayout />,
        children: [
          { path: 'profiles', element: <ProvidersPage /> },
          { path: 'defaults', element: <DefaultsPage /> },
          { path: 'conditions', element: <ConditionsSettingsPage /> },
          { path: 'conditions/:conditionId', element: <ConditionFilePage /> },
          { path: 'research', element: <ResearchFilesPage /> },
          { path: 'freeze', element: <FreezePage /> },
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

const put = (path: string, body: unknown) => fetch(`${location.origin}/api${path}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
const type = (label: string | RegExp, value: string, root: HTMLElement = document.body) => fireEvent.change(within(root).getByLabelText(label), { target: { value } })

describe('settings area', () => {
  it('the old /providers address opens Settings > Profiles, and the tabs name every setting', async () => {
    const router = renderAt('/providers')
    expect(await screen.findByRole('heading', { name: 'Provider profiles' })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/settings/profiles')
    expect(within(screen.getByRole('navigation', { name: 'Settings' })).getAllByRole('link').map((a) => a.textContent)).toEqual(['Profiles', 'Defaults', 'Conditions', 'Research files', 'Freeze'])
  })
})

describe('defaults', () => {
  it('saves the role values and the per-seller overrides; the other roles keep their agents', async () => {
    setMockDefaults({ seller: { profile_id: null, model: null, agents: {} }, judge: { profile_id: 'prof_mock', model: 'mock-model', agents: { J: { profile_id: null, model: 'j' } } } })
    renderAt('/settings/defaults')
    await screen.findByRole('heading', { name: 'Defaults' })
    await waitFor(() => expect(screen.getByLabelText('Seller profile').querySelectorAll('option').length).toBeGreaterThan(1))
    expect(screen.getByLabelText('Judge profile')).toHaveValue('prof_mock')
    type('Seller profile', 'prof_mock')
    type('Seller model', 'mock-model-large')
    fireEvent.click(screen.getByRole('button', { name: 'Add seller override' }))
    type('Agent id', 'B')
    type('Seller B model', 'b-model')
    fireEvent.click(screen.getByRole('button', { name: 'Save defaults' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Saved.')
    expect(sent).toEqual([
      {
        method: 'PUT',
        path: '/api/defaults',
        body: {
          seller: { profile_id: 'prof_mock', model: 'mock-model-large', agents: { B: { profile_id: null, model: 'b-model' } } },
          judge: { profile_id: 'prof_mock', model: 'mock-model', agents: { J: { profile_id: null, model: 'j' } } },
          judge2: { profile_id: null, model: null, agents: {} },
          host_llm: { profile_id: null, model: null, agents: {} },
        },
      },
    ])
  })

  it('refuses a bad or repeated agent id before any request, and marks a default profile the server does not have', async () => {
    setMockDefaults({ seller: { profile_id: 'prof_gone', model: 'x', agents: {} } })
    renderAt('/settings/defaults')
    await screen.findByRole('heading', { name: 'Defaults' })
    await waitFor(() => expect(screen.getByLabelText('Seller profile')).toHaveDisplayValue('prof_gone (not found)'))
    fireEvent.click(screen.getByRole('button', { name: 'Add seller override' }))
    for (const bad of ['seller_1', 'ab', 'b', 'AB']) {
      type('Agent id', bad)
      fireEvent.click(screen.getByRole('button', { name: 'Save defaults' }))
      expect(await screen.findByRole('alert')).toHaveTextContent('A seller id is one capital letter, A to Z')
    }
    fireEvent.click(screen.getByRole('button', { name: 'Add seller override' }))
    screen.getAllByLabelText('Agent id').forEach((el) => fireEvent.change(el, { target: { value: 'A' } }))
    fireEvent.click(screen.getByRole('button', { name: 'Save defaults' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Each agent id can have one override only.')
    expect(sent).toEqual([])
  })
})

describe('research files', () => {
  it('a missing file shows no hash; the server check result shows; a saved file shows its hash and refreshes the freeze inputs', async () => {
    renderAt('/settings/research')
    const rule = await screen.findByRole('region', { name: 'Decision rule' })
    await waitFor(() => expect(within(rule).getByText('Hash').nextSibling).toHaveTextContent('none: the file does not exist yet'))
    const save = within(rule).getByRole('button', { name: 'Save' })
    expect(save).toBeDisabled()
    type('Text', 'rule:\n\tbad: 1', rule)
    fireEvent.click(save)
    expect(await within(rule).findByRole('alert')).toHaveTextContent('Not valid YAML')
    expect(within(rule).getByRole('list', { name: 'Problems in the file' })).toHaveTextContent('text: tab indent')
    type('Text', 'rule:\n  primary: harm\n', rule)
    fireEvent.click(save)
    expect(await within(rule).findByRole('status')).toHaveTextContent('Saved.')
    expect(within(rule).getByText('Hash').nextSibling).toHaveTextContent(/^sha256:mock/)
    expect(sent.map((r) => [r.method, r.path])).toEqual([
      ['PUT', '/api/research/decision-rule'],
      ['PUT', '/api/research/decision-rule'],
    ])
  })
})

describe('research files: the seller models (FLOWS-web D2)', () => {
  it('the page has an editor for the models file, and a save shows its hash and reaches PUT /research/models', async () => {
    renderAt('/settings/research')
    expect((await screen.findAllByRole('heading', { level: 2 })).map((h) => h.textContent)).toEqual(['Decision rule', 'Judge rubric', 'Seller models'])
    const models = await screen.findByRole('region', { name: 'Seller models' })
    await waitFor(() => expect(within(models).getByText('Hash').nextSibling).toHaveTextContent('none: the file does not exist yet'))
    expect(models).toHaveTextContent('conditions/models.yaml')
    expect(models).toHaveTextContent('docs/team/templates/models.template.yaml')
    type('Text', 'id: models-v1\nsellers:\n  A: scripted\n', models)
    fireEvent.click(within(models).getByRole('button', { name: 'Save' }))
    expect(await within(models).findByRole('status')).toHaveTextContent('Saved.')
    expect(within(models).getByText('Hash').nextSibling).toHaveTextContent(/^sha256:mock/)
    expect(sent.map((r) => [r.method, r.path])).toEqual([['PUT', '/api/research/models']])
  })

  it('the freeze page counts the models file as a stage 1 input, and the file is read only once stage 1 is frozen', async () => {
    await put('/research/models', { text: 'id: models-v1\n' })
    await put('/research/judge-rubric', { text: 'rubric: 1\n' })
    const freeze = await (await fetch(`${location.origin}/api/freeze`)).json()
    expect(Object.keys(freeze.inputs)).toContain('models')
    expect(freeze.inputs.models).toMatch(/^sha256:mock/)
    const frozen = await fetch(`${location.origin}/api/freeze`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ stage: 'conditions', approved_by: 'tester' }) })
    expect(frozen.status).toBe(200)
    const refused = await put('/research/models', { text: 'id: models-v2\n' })
    expect(refused.status).toBe(409)
    expect((await refused.json()).code).toBe('research_frozen')
  })

  it('after stage 1 the editor says that freeze stage 1 holds the models file', async () => {
    await put('/research/models', { text: 'id: models-v1\n' })
    await put('/research/judge-rubric', { text: 'rubric: 1\n' })
    await fetch(`${location.origin}/api/freeze`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ stage: 'conditions', approved_by: 'tester' }) })
    renderAt('/settings/research')
    const models = await screen.findByRole('region', { name: 'Seller models' })
    expect(await within(models).findByText('Freeze stage 1 holds this file. It cannot change now.')).toBeInTheDocument()
  })
})

describe('condition files', () => {
  it('lists every condition: a draft can be edited, its save changes the condition hash, and a changed id is refused', async () => {
    renderAt('/settings/conditions')
    const c1 = (await screen.findByRole('link', { name: 'C1' })).closest('li')!
    expect(within(c1).getByRole('link', { name: 'Edit' })).toHaveAttribute('href', '/settings/conditions/C1')
    cleanup()

    renderAt('/settings/conditions/C2a')
    const text = (await screen.findByLabelText('Text')) as HTMLTextAreaElement
    expect(text.value).toMatch(/^id: "C2a"$/m)
    const hashBefore = screen.getByText('Hash').nextSibling!.textContent
    fireEvent.change(text, { target: { value: text.value.replace('id: "C2a"', 'id: "C9"') } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('The file must keep id: C2a.')
    fireEvent.change(text, { target: { value: text.value.replace('id: "C9"', 'id: "C2a"').replace(/^name: .*$/m, 'name: "Common incentives (edited)"') } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Saved.')
    expect(await screen.findByText(/^Common incentives \(edited\)\. Labels/)).toBeInTheDocument()
    expect(screen.getByText('Hash').nextSibling!.textContent).not.toBe(hashBefore)
    const conds = await (await fetch(`${location.origin}/api/conditions`)).json()
    expect(conds.find((c: { id: string }) => c.id === 'C2a').hash).toBe(screen.getByText('Hash').nextSibling!.textContent)
  })
})

describe('freeze', () => {
  const stage1 = () => screen.getByRole('region', { name: 'Stage 1: conditions' })
  const stage2 = () => screen.getByRole('region', { name: 'Stage 2: rule' })
  const confirm = (section: HTMLElement, approver: string) => {
    type('Approved by', approver, section)
    type(`Type ${CONFIRM_WORD} to confirm`, CONFIRM_WORD, section)
  }
  const postStage = (stage: string, approved_by: string) => fetch(`${location.origin}/api/freeze`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ stage, approved_by }) })

  it('stage 1 opens dev: it needs the judge rubric, an approver and the typed word; it freezes the conditions and the rubric, and the rule stays editable', async () => {
    renderAt('/settings/freeze')
    const inputs1 = await screen.findByRole('table', { name: 'Stage 1: conditions inputs' })
    expect(within(inputs1).getByText('condition:C1').nextSibling).toHaveTextContent(/^sha256:/)
    expect(within(inputs1).getByText('judge_rubric').nextSibling).toHaveTextContent('missing')
    expect(within(inputs1).queryByText('decision_rule')).toBeNull()
    const inputs2 = screen.getByRole('table', { name: 'Stage 2: rule inputs' })
    expect(within(inputs2).getAllByRole('row').slice(1).map((r) => r.firstChild!.textContent)).toEqual(['decision_rule'])
    expect(within(stage1()).getByRole('status')).toHaveTextContent('Not frozen. The dev seeds stay locked.')
    expect(within(stage2()).getByRole('status')).toHaveTextContent('Not frozen. The test seeds stay locked.')
    confirm(stage1(), 'Raymond')
    expect(within(stage1()).getByText(/^Missing: judge_rubric, models\./)).toBeInTheDocument()
    expect(within(stage1()).getByRole('button', { name: /Freeze stage 1/ })).toBeDisabled()
    const early = await postStage('conditions', 'Ann')
    expect([early.status, (await early.json()).code]).toEqual([409, 'freeze_incomplete'])
    cleanup()

    await put('/research/judge-rubric', { text: 'rubric: 1\n' })
    await put('/research/models', { text: 'id: models-v1\n' })
    sent.length = 0
    renderAt('/settings/freeze')
    await waitFor(() => expect(within(screen.getByRole('table', { name: 'Stage 1: conditions inputs' })).getByText('judge_rubric').nextSibling).toHaveTextContent(/^sha256:mock/))
    expect(within(stage1()).queryByText(/^Missing:/)).toBeNull()
    const button = within(stage1()).getByRole('button', { name: /Freeze stage 1/ })
    type(`Type ${CONFIRM_WORD} to confirm`, CONFIRM_WORD, stage1())
    expect(button).toBeDisabled()
    type('Approved by', 'Raymond', stage1())
    type(`Type ${CONFIRM_WORD} to confirm`, 'Freeze', stage1())
    expect(button).toBeDisabled()
    type(`Type ${CONFIRM_WORD} to confirm`, CONFIRM_WORD, stage1())
    expect(button).toBeEnabled()
    fireEvent.click(button)
    const stored = await within(stage1()).findByRole('table', { name: 'Frozen hashes' })
    expect(within(stored).getByText('condition:C1')).toBeInTheDocument()
    expect(within(stored).getByText('judge_rubric').nextSibling).toHaveTextContent(/^sha256:mock/)
    expect(within(stored).queryByText('decision_rule')).toBeNull()
    expect(sent).toEqual([{ method: 'POST', path: '/api/freeze', body: { stage: 'conditions', approved_by: 'Raymond' } }])
    await waitFor(() => expect(within(stage1()).getByRole('status')).toHaveTextContent(/^Frozen at .* by Raymond\. The dev seeds are open\.$/))
    expect(within(stage2()).getByRole('status')).toHaveTextContent('Not frozen. The test seeds stay locked.')
    confirm(stage2(), 'Raymond')
    expect(within(stage2()).getByText(/^Missing: decision_rule\./)).toBeInTheDocument()
    expect(within(stage2()).queryByText(/^Freeze stage 1 first/)).toBeNull()
    expect(within(stage2()).getByRole('button', { name: /Freeze stage 2/ })).toBeDisabled()
    cleanup()

    renderAt('/settings/research')
    const rubric = await screen.findByRole('region', { name: 'Judge rubric' })
    expect(await within(rubric).findByText('Freeze stage 1 holds this file. It cannot change now.')).toBeInTheDocument()
    expect(within(rubric).queryByRole('button', { name: 'Save' })).toBeNull()
    const rule = screen.getByRole('region', { name: 'Decision rule' })
    type('Text', 'rule: 1\n', rule)
    fireEvent.click(within(rule).getByRole('button', { name: 'Save' }))
    expect(await within(rule).findByRole('status')).toHaveTextContent('Saved.')
    const refused = await put('/research/judge-rubric', { text: 'rubric: 2\n' })
    expect([refused.status, (await refused.json()).code]).toEqual([409, 'research_frozen'])
    cleanup()

    renderAt('/settings/conditions/C1')
    expect(await screen.findByText('This condition is frozen. It cannot change.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull()
  })

  it('stage 2 waits for stage 1, then opens test; a rule change after it locks test until stage 2 is frozen again, and a new stage 1 removes stage 2', async () => {
    await put('/research/decision-rule', { text: 'rule: 1\n' })
    await put('/research/judge-rubric', { text: 'rubric: 1\n' })
    await put('/research/models', { text: 'id: models-v1\n' })
    const early = await postStage('rule', 'Ann')
    expect([early.status, (await early.json()).code]).toEqual([409, 'conflict'])
    renderAt('/settings/freeze')
    await screen.findByRole('table', { name: 'Stage 1: conditions inputs' })
    confirm(stage2(), 'Raymond')
    expect(within(stage2()).getByText(/^Freeze stage 1 first/)).toBeInTheDocument()
    expect(within(stage2()).queryByText(/^Missing:/)).toBeNull()
    expect(within(stage2()).getByRole('button', { name: /Freeze stage 2/ })).toBeDisabled()
    cleanup()

    await postStage('conditions', 'Ann')
    sent.length = 0
    renderAt('/settings/freeze')
    await waitFor(() => expect(within(stage1()).getByRole('status')).toHaveTextContent(/by Ann\. The dev seeds are open\./))
    confirm(stage2(), 'Raymond')
    expect(within(stage2()).queryByText(/^Freeze stage 1 first/)).toBeNull()
    fireEvent.click(within(stage2()).getByRole('button', { name: /Freeze stage 2/ }))
    const stored = await within(stage2()).findByRole('table', { name: 'Frozen hashes' })
    expect(within(stored).getByText('decision_rule').nextSibling).toHaveTextContent(/^sha256:mock/)
    expect(within(stored).getByText('condition:C1')).toBeInTheDocument()
    expect(sent).toEqual([{ method: 'POST', path: '/api/freeze', body: { stage: 'rule', approved_by: 'Raymond' } }])
    await waitFor(() => expect(within(stage2()).getByRole('status')).toHaveTextContent(/by Raymond\. The test seeds are open\.$/))
    expect(within(stage1()).getByRole('status')).toHaveTextContent(/by Ann\. The dev seeds are open\./)
    cleanup()

    renderAt('/settings/research')
    const rubric = await screen.findByRole('region', { name: 'Judge rubric' })
    expect(await within(rubric).findByText('Freeze stage 1 holds this file. It cannot change now.')).toBeInTheDocument()
    const rule = screen.getByRole('region', { name: 'Decision rule' })
    await waitFor(() => expect(within(rule).getByText(/^Stage 2 is frozen\. A save here makes it not valid/)).toBeInTheDocument())
    await waitFor(() => expect(within(rule).getByLabelText('Text')).toHaveValue('rule: 1\n'))
    type('Text', 'rule: 2\n', rule)
    fireEvent.click(within(rule).getByRole('button', { name: 'Save' }))
    expect(await within(rule).findByRole('alert')).toHaveTextContent('This save made stage 2 not valid. The test seeds are locked until you freeze stage 2 again.')
    type('Text', 'rule: 3\n', rule)
    fireEvent.click(within(rule).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(within(rule).queryByRole('alert')).toBeNull())
    cleanup()

    renderAt('/settings/freeze')
    await waitFor(() => expect(within(stage2()).getByRole('status')).toHaveTextContent(/but it is not valid now\. The test seeds are locked again\./))
    expect(within(stage2()).getByRole('list', { name: 'Stage 2: rule problems' })).toHaveTextContent('decision_rule changed after the freeze')
    confirm(stage2(), 'Mandy')
    fireEvent.click(within(stage2()).getByRole('button', { name: /Freeze stage 2 again/ }))
    await waitFor(() => expect(within(stage2()).getByRole('status')).toHaveTextContent(/by Mandy\. The test seeds are open\.$/))
    confirm(stage1(), 'Ann')
    fireEvent.click(within(stage1()).getByRole('button', { name: /Freeze stage 1 again/ }))
    await waitFor(() => expect(within(stage2()).getByRole('status')).toHaveTextContent('Not frozen. The test seeds stay locked.'))
  })

  it('a frozen stage that is not valid now shows its problems and locks its seeds again', async () => {
    const stage = (over: object) => ({ frozen: true, frozen_at: '2026-10-21T13:00:00Z', approved_by: 'Ann', valid: true, problems: [], ...over })
    server.use(
      (await import('msw/http')).http.get('/api/freeze', () =>
        Response.json({ frozen: true, frozen_at: null, valid: false, problems: [], inputs: {}, stages: { conditions: stage({}), rule: stage({ valid: false, problems: ['features changed after the freeze'] }) } }),
      ),
    )
    renderAt('/settings/freeze')
    await waitFor(() => expect(within(stage2()).getByRole('status')).toHaveTextContent(/by Ann, but it is not valid now\. The test seeds are locked again\./))
    expect(within(stage2()).getByRole('list', { name: 'Stage 2: rule problems' })).toHaveTextContent('features changed after the freeze')
    expect(within(stage1()).getByRole('status')).toHaveTextContent(/The dev seeds are open\./)
    expect(within(stage1()).queryByRole('list')).toBeNull()
  })

  it('shows the server refusal in its stage', async () => {
    await put('/research/judge-rubric', { text: 'rubric: 1\n' })
    await put('/research/models', { text: 'id: models-v1\n' })
    server.use(
      (await import('msw/http')).http.post('/api/freeze', () =>
        Response.json({ title: 'x', status: 409, code: 'hash_mismatch', message: 'Seed scenario scn_s2 does not match the seed map.' }, { status: 409, headers: { 'Content-Type': 'application/problem+json' } }),
      ),
    )
    renderAt('/settings/freeze')
    await screen.findByRole('table', { name: 'Stage 1: conditions inputs' })
    confirm(stage1(), 'Raymond')
    fireEvent.click(within(stage1()).getByRole('button', { name: /Freeze stage 1/ }))
    expect(await within(stage1()).findByRole('alert')).toHaveTextContent('Seed scenario scn_s2 does not match the seed map.')
    expect(screen.queryByRole('table', { name: 'Frozen hashes' })).toBeNull()
  })
})
