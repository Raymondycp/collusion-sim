import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { setupServer } from 'msw/node'
import { http, HttpResponse } from 'msw'
import { useExperiments } from '@/api/experiments'

let statuses: string[] = []
let calls = 0
const server = setupServer(
  http.get('/api/experiments', () => {
    calls += 1
    return HttpResponse.json(statuses.map((status, i) => ({ id: `exp_${i}`, status })))
  }),
)
beforeAll(() => server.listen({ onUnhandledFrame: 'bypass' }))
afterAll(() => server.close())
afterEach(() => {
  cleanup()
  calls = 0
})

function List() {
  const q = useExperiments()
  return <p>{q.data ? 'loaded' : 'loading'}</p>
}

/** List calls in 30 s after the first load. */
async function callsIn30s(list: string[]) {
  statuses = list
  vi.useFakeTimers({ shouldAdvanceTime: true })
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <List />
    </QueryClientProvider>,
  )
  await screen.findByText('loaded')
  const before = calls
  await act(() => vi.advanceTimersByTimeAsync(30_000))
  return calls - before
}

describe('experiment list poll (CR-63 n3)', () => {
  it('every 5 s while a running experiment is listed', async () => {
    expect(await callsIn30s(['blocked', 'running', 'completed'])).toBe(6)
  })

  it('every 15 s when the only open experiments are blocked', async () => {
    expect(await callsIn30s(['blocked', 'ended'])).toBe(2)
  })

  it('off when every experiment is done', async () => {
    expect(await callsIn30s(['completed', 'ended'])).toBe(0)
  })
})
