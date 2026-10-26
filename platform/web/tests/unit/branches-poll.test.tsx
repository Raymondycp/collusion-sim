import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { setupServer } from 'msw/node'
import { http, HttpResponse } from 'msw'
import { useBranches } from '@/api/queries'
import { handlers } from '@/mocks/handlers'
import { findRun, resetMockRuns } from '@/mocks/data'

const asked: string[] = []
const server = setupServer(...handlers)
server.events.on('request:start', ({ request }) => void asked.push(new URL(request.url).pathname))
beforeAll(() => server.listen({ onUnhandledFrame: 'bypass' }))
afterAll(() => server.close())
afterEach(() => {
  cleanup()
  server.resetHandlers()
  resetMockRuns()
  asked.length = 0
})

function Branches({ runId }: { runId: string }) {
  const q = useBranches(runId)
  return <p>{q.data ? `${q.data.length} children` : 'loading'}</p>
}

async function branchCalls(runId: string) {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <Branches runId={runId} />
    </QueryClientProvider>,
  )
  const path = `/api/runs/${runId}/branches`
  await waitFor(() => expect(asked).toContain(`/api/runs/${runId}`))
  await waitFor(() => expect(asked.filter((p) => p === path).length).toBeGreaterThan(0))
  // One more poll after the run record loads can come; after that the count must stay.
  await act(() => vi.advanceTimersByTimeAsync(6_000))
  const before = asked.filter((p) => p === path).length
  await act(() => vi.advanceTimersByTimeAsync(20_000))
  return { before, after: asked.filter((p) => p === path).length }
}

describe('branch list poll (A7 finding 4)', () => {
  it('stops for an ended branch run: no experiment can add a child to it', async () => {
    const { before, after } = await branchCalls('run_MOCK01PROBE')
    expect(after).toBe(before)
  })

  // run_MOCK01 has ended and its two children have ended. Only its experiment can still add a child.
  const inExperiment = (status: string) => {
    findRun('run_MOCK01')!.experimentId = 'exp_P'
    server.use(http.get('/api/experiments', () => HttpResponse.json([{ id: 'exp_P', status }])))
  }

  it('goes on for an ended parent run while its experiment runs (CR-63 2)', async () => {
    inExperiment('running')
    const { before, after } = await branchCalls('run_MOCK01')
    expect(after).toBeGreaterThanOrEqual(before + 3)
  })

  it('stops for an ended parent run once its experiment is done (CR-63 2)', async () => {
    inExperiment('completed')
    const { before, after } = await branchCalls('run_MOCK01')
    expect(after).toBe(before)
  })

  it('stops when the experiment becomes done while the page is open: the experiment list polls (CR-63 n2)', async () => {
    let status = 'running'
    findRun('run_MOCK01')!.experimentId = 'exp_P'
    server.use(http.get('/api/experiments', () => HttpResponse.json([{ id: 'exp_P', status }])))
    const path = '/api/runs/run_MOCK01/branches'
    const { before, after } = await branchCalls('run_MOCK01')
    expect(after).toBeGreaterThanOrEqual(before + 3)
    status = 'completed'
    await act(() => vi.advanceTimersByTimeAsync(12_000))
    const done = asked.filter((p) => p === path).length
    await act(() => vi.advanceTimersByTimeAsync(20_000))
    expect(asked.filter((p) => p === path).length).toBe(done)
  })

  it('goes on for a live run', async () => {
    const { before, after } = await branchCalls('run_MOCK02')
    expect(after).toBeGreaterThanOrEqual(before + 3)
  })
})
