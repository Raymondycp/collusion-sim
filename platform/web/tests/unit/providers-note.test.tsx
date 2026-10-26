import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { http, HttpResponse } from 'msw/http'
import { setupServer } from 'msw/node'
import { ProvidersPage } from '@/features/providers/ProvidersPage'
import { providerHandlers, resetMockProfiles, setMockPaidCalls } from '@/mocks/providers'

const server = setupServer(...providerHandlers)
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }))
afterAll(() => server.close())
afterEach(() => {
  cleanup()
  server.resetHandlers()
  resetMockProfiles()
  vi.unstubAllEnvs()
})

const renderPage = () =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ProvidersPage />
    </QueryClientProvider>,
  )

describe('providers page without a key store', () => {
  it('on the mock, says not to type a real key', async () => {
    vi.stubEnv('MODE', 'mock')
    renderPage()
    expect(await screen.findByRole('note', { name: 'No key store' })).toHaveTextContent('Mock backend. Do not type a real API key here.')
    expect(screen.getByRole('button', { name: /Add profile/ })).toBeEnabled()
  })

  it('on a backend without the profile endpoints, says so and turns off Add', async () => {
    server.use(http.get('/api/provider-profiles', () => HttpResponse.json({ detail: 'Not Found' }, { status: 404 })))
    renderPage()
    expect(await screen.findByRole('note', { name: 'No key store' })).toHaveTextContent('This backend has no key store yet. Do not add a profile.')
    expect(screen.getByRole('button', { name: /Add profile/ })).toBeDisabled()
  })

  it('on a backend with a key store, shows no note', async () => {
    renderPage()
    expect(await screen.findByText('Mock endpoint')).toBeInTheDocument()
    expect(screen.queryByRole('note', { name: 'No key store' })).toBeNull()
  })

  it('supports_seed: the checkbox sets it, and the profile says whether the seed is sent', async () => {
    renderPage()
    const row = (await screen.findByText('Mock endpoint')).closest('li')!
    expect(within(row).getByText('Seed parameter').nextSibling).toHaveTextContent('not sent')
    fireEvent.click(screen.getByRole('button', { name: 'Edit Mock endpoint' }))
    const dialog = screen.getByRole('dialog')
    fireEvent.click(within(dialog).getByRole('checkbox', { name: /honours the seed parameter/ }))
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(within(screen.getByText('Mock endpoint').closest('li')!).getByText('Seed parameter').nextSibling).toHaveTextContent('sent with each call'))
  })

  it('names the env var a key can also come from, per profile', async () => {
    renderPage()
    const keyring = (await screen.findByText('Mock endpoint')).closest('li')!
    expect(keyring).toHaveTextContent('in the OS keyring (a server with SIM_KEYRING=1 is needed). Or set SIM_KEY_PROF_MOCK in the server .env; the keyring key wins.')
    const env = screen.getByText('Local vLLM (.env)').closest('li')!
    expect(env).toHaveTextContent('from SIM_PROFILE_ENV_LOCAL_KEY in the server .env (read-only)')
  })

  it('a failed test shows the HTTP status and what the provider said (contract 0.34.0)', async () => {
    server.use(
      http.post('/api/provider-profiles/:id/test', () =>
        HttpResponse.json({ ok: false, model: 'm-large', latency_ms: null, supports: null, error: { kind: 'auth', http_status: 401, message: 'The endpoint refused the key.', provider_code: 'invalid_api_key', provider_message: 'Incorrect API key provided: sk-…****. Model m-large stays.' } }),
      ),
    )
    renderPage()
    const card = (await screen.findByText('Mock endpoint')).closest('li')!
    fireEvent.click(within(card).getByRole('button', { name: /Test \(1 call\)/ }))
    const alert = await within(card).findByRole('alert')
    expect(alert).toHaveTextContent('Failed with m-large: auth (HTTP 401). The endpoint refused the key.')
    expect(alert).toHaveTextContent('The provider said: invalid_api_key: Incorrect API key provided: sk-…****. Model m-large stays.')
  })

  it('a failed test with no provider words shows only our summary', async () => {
    server.use(http.post('/api/provider-profiles/:id/test', () => HttpResponse.json({ ok: false, model: 'm', latency_ms: null, supports: null, error: { kind: 'network', http_status: null, message: 'No answer.', provider_code: null, provider_message: null } })))
    renderPage()
    const card = (await screen.findByText('Mock endpoint')).closest('li')!
    fireEvent.click(within(card).getByRole('button', { name: /Test \(1 call\)/ }))
    const alert = await within(card).findByRole('alert')
    expect(alert).toHaveTextContent('Failed with m: network. No answer.')
    expect(alert).not.toHaveTextContent('The provider said')
  })

  it('a test call on a server with paid calls off says so', async () => {
    setMockPaidCalls(false)
    renderPage()
    const card = (await screen.findByText('Mock endpoint')).closest('li')!
    fireEvent.click(within(card).getByRole('button', { name: /Test \(1 call\)/ }))
    expect(await within(card).findByRole('alert')).toHaveTextContent('Paid calls are off on this server. The lead can turn them on (SIM_ALLOW_PAID=1).')
  })
})
