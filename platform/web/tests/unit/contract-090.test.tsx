import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { setupServer } from 'msw/node'
import { capsText, EstimateNotes, spendText } from '@/components/budget'
import { missingDefaultProfile, newAgent, type Registries } from '@/features/new-run/draft'
import { ProvidersPage } from '@/features/providers/ProvidersPage'
import { foldEvents } from '@/run/fold'
import { makeEpisode } from '@/mocks/episode'
import { providerHandlers, resetMockProfiles } from '@/mocks/providers'
import { mockPolicies, mockRoles, mockRunConfigSchema, mockTools } from '@/mocks/registry'
import { mockScenarioSummaries } from '@/mocks/scenarios'
import type { SimEvent } from '@/api/types'

const server = setupServer(...providerHandlers)
const sent: unknown[] = []
server.events.on('request:start', async ({ request }) => {
  if (request.method !== 'GET') sent.push(await request.clone().json())
})
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }))
afterAll(() => server.close())
afterEach(() => {
  cleanup()
  server.resetHandlers()
  resetMockProfiles()
  sent.length = 0
})

const spend = { calls: 1200, tokens: 2_640_000, usd: 0.5, unpriced: 0, usd_complete: true }

describe('budget caps and spend (contract 0.9.0)', () => {
  it('names every cap that is set, and only those', () => {
    expect(capsText({ max_usd: 0.5, max_calls: 200, max_tokens: 1_000_000 })).toBe('$0.5, 200 calls, 1,000,000 tokens')
    expect(capsText({ max_tokens: 5000 })).toBe('5,000 tokens')
    expect(capsText({ max_usd: 0 })).toBe('$0')
    expect(capsText({})).toBe('no cap')
  })

  it('spend: dollars are a lower bound when some calls have no price, and those calls are counted', () => {
    expect(spendText(spend, { max_calls: 5000, max_tokens: null, max_usd: 2 })).toBe('1,200 of 5,000 calls, 2,640,000 tokens, $0.50 of $2.00')
    expect(spendText({ ...spend, unpriced: 300, usd_complete: false })).toBe('1,200 calls, 2,640,000 tokens, at least $0.50, 300 calls with no price')
  })

  it('the estimate shows totals, each 5-hour window and every server warning', () => {
    render(
      <EstimateNotes
        calls={1500}
        tokensMax={{ input: 1_000_000, output: 50_000 }}
        rateWindow={[
          { profile_id: 'prof_go', max_calls_per_5h: 300, used: 250, room: 50, rate_limited: 0, calls_max: 1500, enough: false },
          { profile_id: 'prof_big', max_calls_per_5h: 5000, used: 0, room: 5000, rate_limited: 0, calls_max: 10, enough: true },
        ]}
        warnings={['Profile prof_go: its 5-hour window has room for 50 calls; the worst case needs 1500.']}
      />,
    )
    expect(screen.getByText(/at most 1,500 calls and 1,050,000 tokens/)).toBeInTheDocument()
    const rows = within(screen.getByRole('list', { name: '5-hour call windows' })).getAllByRole('listitem')
    expect(rows.map((r) => r.textContent)).toEqual([
      'prof_go: 50 of 300 calls left in its 5-hour window (this machine, calls in flight included), up to 1500 needed',
      'prof_big: 5000 of 5000 calls left in its 5-hour window (this machine, calls in flight included), up to 10 needed',
    ])
    expect(rows[0]).toHaveClass('text-amber-700')
    expect(rows[1]).not.toHaveClass('text-amber-700')
    expect(screen.getByRole('list', { name: 'Estimate warnings' })).toHaveTextContent('room for 50 calls')
  })

  it('no window rows and no warnings: neither list is shown', () => {
    render(<EstimateNotes calls={0} tokensMax={{}} rateWindow={[]} warnings={[]} />)
    expect(screen.queryByRole('list')).toBeNull()
  })
})

describe('run state from 0.9.0 events', () => {
  const ep = makeEpisode({ runId: 'run_T9', kind: 'exploratory', seed: 7, rounds: 30, start: '2026-10-23T09:00:00Z', conditionId: null, branch: null })
  const calls = ep.filter((e) => e.type === 'llm_call')

  const paid = calls.filter((e) => !e.cache_hit || e.charged)

  it('sums the tokens of every kind that the budget counts, and counts each limit that a turn hit', () => {
    const s = foldEvents(ep)
    expect(paid.length).toBeGreaterThan(0)
    expect(paid.length).toBeLessThan(calls.length)
    expect(s.tokens).toBe(paid.reduce((n, e) => n + (e.input_tokens ?? 0) + (e.output_tokens ?? 0), 0))
    expect(s.cachedInputTokens).toBe(paid.reduce((n, e) => n + (e.cached_input_tokens ?? 0), 0))
    expect(s.reasoningTokens).toBe(paid.reduce((n, e) => n + (e.reasoning_tokens ?? 0), 0))
    expect(s.cachedInputTokens).toBeGreaterThan(0)
    const hits = ep.filter((e) => e.type === 'acted' && e.limits_hit?.includes('max_steps_per_turn')).length
    expect(hits).toBeGreaterThan(0)
    expect(s.limitsHit).toEqual({ max_steps_per_turn: hits })
    expect(s.unpricedCalls).toBe(0)
  })

  it('a paid call or a charged replay with no price makes the cost a lower bound; a plain cache hit does not (CR-24 m1)', () => {
    const [hit, replay, call] = calls
    const changed = new Map<SimEvent, SimEvent>([
      [hit!, { ...hit!, cache_hit: true, charged: false, cost_usd: null }],
      [replay!, { ...replay!, cache_hit: true, charged: true, cost_usd: null }],
      [call!, { ...call!, cache_hit: false, charged: true, cost_usd: null }],
    ])
    const before = foldEvents(ep)
    const after = foldEvents(ep.map((e) => changed.get(e) ?? e))
    expect(after.unpricedCalls).toBe(2)
    const tokensOf = (e: SimEvent) => (e.type === 'llm_call' ? (e.input_tokens ?? 0) + (e.output_tokens ?? 0) : 0)
    const three = [hit!, replay!, call!]
    const beforeThree = three.filter((e) => paid.includes(e as (typeof paid)[number])).reduce((n, e) => n + tokensOf(e), 0)
    expect(after.tokens).toBe(before.tokens - beforeThree + tokensOf(replay!) + tokensOf(call!))
  })

  it('a budget marker names the dollars only when the budget has max_usd (CR-24 m2)', () => {
    const base = { seq: ep[0]!.seq + 1, run_id: 'run_T9', t: 5, wall_time: '2026-10-23T09:05:00Z', tier: 'B' as const, type: 'budget_warning' as const }
    const text = (e: Record<string, unknown>) => foldEvents([...ep.slice(0, 1), e as SimEvent]).markers.find((m) => m.kind === 'budget')?.text
    expect(text({ ...base, level: 'p100', spent_usd: 0, max_usd: 0 })).toBe('Budget reached (call or token cap)')
    expect(text({ ...base, level: 'p80', spent_usd: 0, max_usd: 0 })).toBe('Budget at 80% (call or token cap)')
    expect(text({ ...base, level: 'p100', spent_usd: 0.5, max_usd: 0.5 })).toBe('Budget reached: $0.50 of $0.50')
  })
})

describe('defaults pre-fill (GET /defaults)', () => {
  const profile = (id: string, model: string) => ({ id, name: id, base_url: 'http://x', key_set: true, key_hint: null, key_store: 'keyring' as const, supports_seed: false, created_at: '', default_model: model, supports: { tool_calls: null, json_schema: null, checked_at: null }, key_env: `SIM_KEY_${id.toUpperCase()}` })
  const reg: Registries = {
    root: mockRunConfigSchema,
    roles: mockRoles,
    tools: mockTools,
    policies: mockPolicies,
    profiles: [profile('prof_a', 'a-model'), profile('prof_b', 'b-model')],
    scenarios: mockScenarioSummaries(),
    defaults: {
      seller: { profile_id: 'prof_b', model: 'role-model', agents: { B: { profile_id: 'prof_a', model: 'b-own' } } },
      judge: { profile_id: null, model: null, agents: {} },
      judge2: { profile_id: null, model: null, agents: {} },
      host_llm: { profile_id: null, model: null, agents: {} },
    },
  }
  const llmOf = (a: ReturnType<typeof newAgent>) => [a.policy.llm?.provider_profile_id, a.policy.llm?.model]
  const llmSeller = (agents: ReturnType<typeof newAgent>[], research: boolean) =>
    newAgent('seller', agents, { ...reg, policies: reg.policies.filter((p) => p.kind === 'llm') }, research)

  it('an exploratory seller takes its own default first, then the role default', () => {
    const a = llmSeller([], false)
    expect(a.id).toBe('A')
    expect(llmOf(a)).toEqual(['prof_b', 'role-model'])
    expect(llmOf(llmSeller([a], false))).toEqual(['prof_a', 'b-own'])
  })

  it('a default profile that the server does not have drops the default model too, and the editor can say so (CR-24 m3)', () => {
    const gone: Registries = { ...reg, defaults: { ...reg.defaults!, seller: { profile_id: 'prof_gone', model: 'gone-model', agents: { B: { profile_id: null, model: 'b-own' } } } } }
    const seller = (agents: ReturnType<typeof newAgent>[]) => newAgent('seller', agents, { ...gone, policies: gone.policies.filter((p) => p.kind === 'llm') }, false)
    const a = seller([])
    expect(llmOf(a)).toEqual(['prof_a', 'a-model'])
    expect(llmOf(seller([a]))).toEqual(['prof_a', 'a-model'])
    expect(missingDefaultProfile(gone, 'A')).toBe('prof_gone')
    expect(missingDefaultProfile(reg, 'A')).toBeNull()
    expect(missingDefaultProfile(reg, 'B')).toBeNull()
  })

  it('a research seller ignores the defaults', () => {
    const a = llmSeller([], true)
    expect(llmOf(a)).not.toContain('role-model')
    expect(llmOf(llmSeller([a], true))).not.toContain('b-own')
  })
})

const renderProviders = () =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ProvidersPage />
    </QueryClientProvider>,
  )

describe('provider profiles (contract 0.9.0)', () => {
  it('a .env profile is read-only: badge and note, no Edit or Delete, the test call stays', async () => {
    renderProviders()
    const env = (await screen.findByText('Local vLLM (.env)')).closest('li')!
    expect(env).toHaveTextContent('.env, read-only')
    expect(env).toHaveTextContent('This profile comes from the server .env. Change it there and restart the server.')
    expect(within(env).queryByRole('button', { name: /Edit|Delete/ })).toBeNull()
    expect(within(env).getByRole('button', { name: /Test \(1 call\)/ })).toBeInTheDocument()
    expect(within(env).getByText('Session header').nextSibling).toHaveTextContent('x-opencode-session')
    expect(within(env).getByText('5-hour window').nextSibling).toHaveTextContent('no limit')
    expect(within(env).getByText('Price (USD / M tokens)').nextSibling).toHaveTextContent('not set: cost not counted; runs need a call or token cap')

    const store = screen.getByText('Mock endpoint').closest('li')!
    expect(store).not.toHaveTextContent('read-only')
    expect(within(store).getByRole('button', { name: 'Edit Mock endpoint' })).toBeInTheDocument()
    expect(within(store).getByText('5-hour window').nextSibling).toHaveTextContent('12 of 300 calls used, 288 left')
    expect(within(store).getByText('Protocol').nextSibling).toHaveTextContent('OpenAI Chat Completions')
  })

  it('add: sends protocol, 5-hour cap and session header; the unbuilt protocol cannot be chosen', async () => {
    renderProviders()
    fireEvent.click(await screen.findByRole('button', { name: /Add profile/ }))
    const dialog = screen.getByRole('dialog')
    const protocol = within(dialog).getByLabelText('Protocol') as HTMLSelectElement
    expect([...protocol.options].filter((o) => o.disabled).map((o) => o.value)).toEqual(['typesafe_systemone'])
    fireEvent.change(within(dialog).getByLabelText('Name'), { target: { value: 'Go' } })
    fireEvent.change(within(dialog).getByLabelText('Base URL'), { target: { value: 'https://opencode.ai/zen/go/v1' } })
    fireEvent.change(within(dialog).getByLabelText(/API key/), { target: { value: 'test-not-a-key' } })
    fireEvent.change(protocol, { target: { value: 'anthropic_messages' } })
    fireEvent.change(within(dialog).getByLabelText('Calls per 5 hours'), { target: { value: '300' } })
    fireEvent.change(within(dialog).getByLabelText('Session header'), { target: { value: 'x-opencode-session' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Add profile' }))
    await waitFor(() => expect(sent).toHaveLength(1))
    expect(sent[0]).toMatchObject({ protocol: 'anthropic_messages', max_calls_per_5h: 300, session_header: 'x-opencode-session' })
    const card = (await screen.findByText('Go')).closest('li')!
    expect(within(card).getByText('Protocol').nextSibling).toHaveTextContent('Anthropic Messages')
  })

  it('empty 5-hour cap and session header send null; a bad header name is refused before any request', async () => {
    renderProviders()
    fireEvent.click(await screen.findByRole('button', { name: 'Edit Mock endpoint' }))
    const dialog = screen.getByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText('Session header'), { target: { value: 'x session' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('The session header must be a header name')
    fireEvent.change(within(dialog).getByLabelText('Calls per 5 hours'), { target: { value: '0' } })
    fireEvent.change(within(dialog).getByLabelText('Session header'), { target: { value: '' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Calls per 5 hours must be a whole number of 1 or more, or empty.')
    expect(sent).toEqual([])
    fireEvent.change(within(dialog).getByLabelText('Calls per 5 hours'), { target: { value: '' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(sent).toHaveLength(1))
    expect(sent[0]).toMatchObject({ max_calls_per_5h: null, session_header: null })
  })
})
