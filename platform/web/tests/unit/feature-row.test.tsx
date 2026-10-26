import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { focusManager, QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { setupServer } from 'msw/node'
import { delay, http, HttpResponse } from 'msw'
import { useFeatureRow } from '@/features/workbench/metrics'

let status = 'running'
let tables = 0
let tableDelay = 0
const asked: string[] = []
const searches: string[] = []
const server = setupServer(
  http.get('/api/experiments', () => HttpResponse.json([{ id: 'exp_F', status }])),
  http.get('/api/experiments/exp_F/features', async () => {
    tables += 1
    const n = tables
    await delay(tableDelay)
    return HttpResponse.json({ columns: [], rows: [{ parent_run_id: 'run_F', msg_volume: n }] })
  }),
)
server.events.on('request:start', ({ request }) => {
  asked.push(new URL(request.url).pathname)
  searches.push(new URL(request.url).search)
})
beforeAll(() => server.listen({ onUnhandledFrame: 'bypass' }))
afterAll(() => server.close())
afterEach(() => {
  cleanup()
  focusManager.setFocused(undefined)
  status = 'running'
  tables = 0
  tableDelay = 0
  asked.length = 0
  searches.length = 0
})

const shown: string[] = []
function Row() {
  const row = useFeatureRow('run_F', 'exp_F', null)
  const text = row.data ? `table ${row.data.msg_volume?.value}` : 'loading'
  shown.push(text)
  return <p>{text}</p>
}

/** Mounts the row, unmounts it, and mounts it again with the same cache, as a tab change does. */
function openTwice(qc: QueryClient) {
  const view = () => (
    <QueryClientProvider client={qc}>
      <Row />
    </QueryClientProvider>
  )
  const first = render(view())
  return { first, again: () => (first.unmount(), render(view())) }
}

const client = () => new QueryClient({ defaultOptions: { queries: { retry: false } } })

describe('feature table cache (CR-63 1)', () => {
  it('a running experiment: each open reads the table again', async () => {
    const { again } = openTwice(client())
    await screen.findByText('table 1')
    again()
    await screen.findByText('table 2')
  })

  it('a done experiment: the table is read once', async () => {
    status = 'completed'
    const { again } = openTwice(client())
    await screen.findByText('table 1')
    again()
    await screen.findByText('table 1')
    await new Promise((r) => setTimeout(r, 100))
    expect(tables).toBe(1)
  })

  it('the table is read again when the experiment becomes done, and the old values stay until it arrives (CR-63 n1)', async () => {
    const qc = client()
    openTwice(qc)
    await screen.findByText('table 1')
    tableDelay = 150
    shown.length = 0
    status = 'ended'
    await act(() => qc.invalidateQueries({ queryKey: ['experiments'] }))
    await waitFor(() => expect(screen.getByText('table 2')).toBeInTheDocument())
    expect(shown).not.toContain('loading')
  })

  it('a window focus does not read the table again (CR-63 n1)', async () => {
    openTwice(client())
    await screen.findByText('table 1')
    act(() => {
      focusManager.setFocused(false)
      focusManager.setFocused(true)
    })
    await new Promise((r) => setTimeout(r, 100))
    expect(tables).toBe(1)
  })

  it('the status comes from the experiment list, not from the experiment detail (CR-63 n2)', async () => {
    openTwice(client())
    await screen.findByText('table 1')
    expect(asked).toContain('/api/experiments')
    expect(asked).not.toContain('/api/experiments/exp_F')
  })
})

describe('feature table with a judge job (0.24.0, CR-65 2)', () => {
  function Judged() {
    const row = useFeatureRow('run_F', 'exp_F', 'eval_1')
    return <p>{row.data ? 'loaded' : 'loading'}</p>
  }

  it('sends the evaluation id as the repeatable evaluation_id parameter', async () => {
    render(
      <QueryClientProvider client={client()}>
        <Judged />
      </QueryClientProvider>,
    )
    await screen.findByText('loaded')
    expect(searches.filter((q) => q.includes('layer'))).toEqual(['?layer=B&evaluation_id=eval_1'])
  })
})
