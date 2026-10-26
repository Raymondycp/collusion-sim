import { inspect } from 'node:util'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { http, HttpResponse } from 'msw/http'
import { setupServer } from 'msw/node'
import { ProvidersPage } from '@/features/providers/ProvidersPage'
import { providerHandlers, resetMockProfiles } from '@/mocks/providers'

const KEY = 'sk-LEAKTEST-aaaa-1111-bbbb-6789'
const KEY2 = 'sk-LEAKTEST-cccc-2222-dddd-4321'

type Seen = { method: string; url: string; body: string }
const requests: Seen[] = []
const responses: string[] = []
const consoleOut: string[] = []

const server = setupServer(...providerHandlers)
server.events.on('request:start', async ({ request }) => {
  requests.push({ method: request.method, url: request.url, body: await request.clone().text() })
})
server.events.on('response:mocked', async ({ response }) => {
  responses.push(await response.clone().text())
})

let qc: QueryClient

beforeAll(() => server.listen({ onUnhandledFrame: 'error' }))
afterAll(() => server.close())
beforeEach(() => {
  requests.length = 0
  responses.length = 0
  consoleOut.length = 0
  resetMockProfiles()
  for (const m of ['log', 'info', 'warn', 'error', 'debug'] as const) {
    vi.spyOn(console, m).mockImplementation((...args: unknown[]) => void consoleOut.push(args.map((a) => (typeof a === 'string' ? a : inspect(a, { depth: 6 }))).join(' ')))
  }
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <ProvidersPage />
    </QueryClientProvider>,
  )
})
afterEach(() => {
  cleanup()
  server.resetHandlers()
  vi.restoreAllMocks()
})

function fill(label: RegExp, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } })
}

async function addProfile(key: string) {
  fireEvent.click(await screen.findByRole('button', { name: 'Add profile' }))
  fill(/^Name/, 'Leak test')
  fill(/^Base URL/, 'https://example.invalid/v1')
  fill(/^API key/, key)
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Add profile' }))
}

/** Every place in the browser where a key could stay after the request. */
function placesHolding(key: string) {
  const caches = JSON.stringify({
    queries: qc.getQueryCache().getAll().map((q) => [q.queryKey, q.state.data, q.state.error]),
    mutations: qc.getMutationCache().getAll().map((m) => [m.state.variables, m.state.data, m.state.error]),
  })
  const where: Record<string, string> = {
    localStorage: JSON.stringify({ ...localStorage }),
    sessionStorage: JSON.stringify({ ...sessionStorage }),
    url: location.href,
    dom: document.documentElement.outerHTML + [...document.querySelectorAll('input')].map((i) => i.value).join('\n'),
    queryAndMutationCache: caches,
    console: consoleOut.join('\n'),
    responses: responses.join('\n'),
    requestUrls: requests.map((r) => r.url).join('\n'),
  }
  return Object.keys(where).filter((k) => where[k]!.includes(key))
}

describe('provider profile key handling', () => {
  it('sends a pasted key without the spaces and line break around it', async () => {
    await addProfile(`  ${KEY}
`)
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    const post = requests.find((r) => r.method === 'POST')!
    expect(JSON.parse(post.body).api_key).toBe(KEY)
  })

  it('sends a new key once, in the POST body only, and keeps it nowhere in the browser', async () => {
    await addProfile(KEY)
    expect(await screen.findByText('sk-…6789')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())

    const carrying = requests.filter((r) => r.body.includes(KEY))
    expect(carrying).toEqual([expect.objectContaining({ method: 'POST', url: expect.stringMatching(/\/api\/provider-profiles$/) })])
    expect(placesHolding(KEY)).toEqual([])
  })

  it('sends a replacement key once, in the PATCH body only, and an edit without a key sends no api_key field', async () => {
    fireEvent.click(await screen.findByRole('button', { name: 'Edit Mock endpoint' }))
    fill(/^New API key/, KEY2)
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Save' }))
    expect(await screen.findByText('sk-…4321')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())

    expect(requests.filter((r) => r.body.includes(KEY2))).toEqual([expect.objectContaining({ method: 'PATCH', url: expect.stringMatching(/\/api\/provider-profiles\/prof_mock$/) })])
    expect(placesHolding(KEY2)).toEqual([])

    fireEvent.click(screen.getByRole('button', { name: 'Edit Mock endpoint' }))
    fill(/^Name/, 'Mock endpoint 2')
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Save' }))
    await screen.findByText('Mock endpoint 2')
    const patch = requests.filter((r) => r.method === 'PATCH').at(-1)!
    expect(JSON.parse(patch.body)).not.toHaveProperty('api_key')
  })

  it('cuts the key out of an error message, if a faulty backend echoes it', async () => {
    server.use(
      http.post('*/api/provider-profiles', async ({ request }) => {
        const { api_key } = (await request.json()) as { api_key: string }
        return HttpResponse.json({ title: 'x', status: 422, code: 'validation_error', message: `Bad key ${api_key}`, errors: [] }, { status: 422 })
      }),
    )
    await addProfile(KEY)
    expect(await screen.findByText('Bad key [key]')).toBeInTheDocument()
    responses.length = 0
    const left = placesHolding(KEY)
    // The dialog stays open on an error, so the person can correct the form. The key is still in its password input.
    expect(left).toEqual(['dom'])
    expect(document.documentElement.outerHTML).not.toContain(KEY)
  })
})
