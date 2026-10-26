import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { setupServer } from 'msw/node'
import type { RunStatus } from '@/api/types'
import { handlers } from '@/mocks/handlers'
import { addMockRun, controlMockRun, findRun, LIVE_MS, visibleEvents } from '@/mocks/data'
import { RunControls } from '@/features/run-view/RunControls'

const posts: string[] = []
const server = setupServer(...handlers)
server.events.on('request:start', ({ request }) => void (request.method === 'POST' && posts.push(new URL(request.url).pathname)))
beforeAll(() => server.listen({ onUnhandledFrame: 'bypass' }))
afterEach(() => {
  cleanup()
  posts.length = 0
})
afterAll(() => server.close())

function renderControls(runId: string, status: RunStatus, split: string | null | undefined) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const ui = (s: RunStatus) => (
    <QueryClientProvider client={qc}>
      <RunControls runId={runId} status={s} split={split} />
    </QueryClientProvider>
  )
  const { rerender } = render(ui(status))
  return (s: RunStatus) => rerender(ui(s))
}

const buttons = () => screen.queryAllByRole('button').map((b) => b.textContent?.trim())

describe('run controls', () => {
  it.each([
    ['running', ['Pause', 'Stop']],
    ['paused', ['Resume', 'Stop']],
    ['queued', ['Stop']],
    ['completed', []],
    ['stopped', []],
    ['recovering', []],
  ] as const)('a %s run offers %j', (status, shown) => {
    renderControls('run_X', status, 'exploratory')
    expect(buttons()).toEqual(shown)
  })

  it('pause sends one POST to /pause', async () => {
    const id = addMockRun({ runId: 'run_CTRL1', kind: 'exploratory', seed: 1, rounds: 30, start: new Date().toISOString(), conditionId: null, branch: null })
    renderControls(id, 'running', 'exploratory')
    fireEvent.click(screen.getByRole('button', { name: 'Pause' }))
    await waitFor(() => expect(posts).toEqual([`/api/runs/${id}/pause`]))
  })

  it('stop asks first, and sends nothing on Cancel', async () => {
    const id = addMockRun({ runId: 'run_CTRL2', kind: 'exploratory', seed: 1, rounds: 30, start: new Date().toISOString(), conditionId: null, branch: null })
    renderControls(id, 'running', 'exploratory')
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }))
    expect(posts).toEqual([])
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Stop run' }))
    await waitFor(() => expect(posts).toEqual([`/api/runs/${id}/stop`]))
  })

  it("shows the backend's message when the run has already ended", async () => {
    renderControls('run_MOCK01', 'running', 'exploratory')
    fireEvent.click(screen.getByRole('button', { name: 'Pause' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Run run_MOCK01 has ended.')
  })

  it('removes the error when the stream shows a new status', async () => {
    const setStatus = renderControls('run_MOCK01', 'running', 'exploratory')
    fireEvent.click(screen.getByRole('button', { name: 'Pause' }))
    await screen.findByRole('alert')
    setStatus('completed')
    expect(screen.queryByRole('alert')).toBeNull()
  })
})

describe('mock run control, like the backend', () => {
  const live = (id: string) => findRun(addMockRun({ runId: id, kind: 'exploratory', seed: 1, rounds: 30, start: new Date().toISOString(), conditionId: null, branch: null }))!

  it('pause adds run_status_changed paused and freezes the run; a second pause changes nothing', async () => {
    const r = live('run_CTRL3')
    expect(controlMockRun(r, 'pause')).toBe(true)
    const shown = visibleEvents(r)
    expect(shown.at(-1)).toMatchObject({ type: 'run_status_changed', status: 'paused', seq: shown.length - 1 })
    expect(controlMockRun(r, 'pause')).toBe(true)
    await new Promise((res) => setTimeout(res, LIVE_MS * 3))
    expect(visibleEvents(r)).toEqual(shown)
  })

  it('resume adds run_status_changed running and the run moves on, with seq still gap-free', async () => {
    const r = live('run_CTRL4')
    controlMockRun(r, 'pause')
    controlMockRun(r, 'resume')
    const at = visibleEvents(r).length
    expect(visibleEvents(r).at(-1)).toMatchObject({ type: 'run_status_changed', status: 'running' })
    await new Promise((res) => setTimeout(res, LIVE_MS * 3))
    expect(visibleEvents(r).length).toBeGreaterThan(at)
    expect(r.events.map((e) => e.seq)).toEqual(r.events.map((_, i) => i))
  })

  it('stop ends the run with run_ended stopped; a later control is refused', () => {
    const r = live('run_CTRL5')
    expect(controlMockRun(r, 'stop')).toBe(true)
    expect(r.events.at(-1)).toMatchObject({ type: 'run_ended', status: 'stopped', end_reason: 'stopped_by_user' })
    expect(controlMockRun(r, 'pause')).toBe(false)
  })
})

describe('stop on a research run (CR-49 2)', () => {
  const bodies: string[] = []
  beforeAll(() => {
    server.events.on('request:start', async ({ request }) => {
      if (new URL(request.url).pathname.endsWith('/stop')) bodies.push(await request.clone().text())
    })
  })
  afterEach(() => void (bodies.length = 0))

  it('a test run says the cell stays locked, needs a typed reason, and sends it with the stop', async () => {
    const id = addMockRun({ runId: 'run_CTRL_TEST', kind: 'exploratory', seed: 1, rounds: 30, start: new Date().toISOString(), conditionId: null, branch: null })
    renderControls(id, 'running', 'test')
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
    const dialog = within(screen.getByRole('dialog'))
    expect(dialog.getByText(/test cell stays locked: it cannot be run again/)).toBeInTheDocument()
    const stop = dialog.getByRole('button', { name: 'Stop run' })
    expect(stop).toBeDisabled()
    fireEvent.change(dialog.getByRole('textbox', { name: /Reason for the stop/ }), { target: { value: '   ' } })
    expect(stop).toBeDisabled()
    fireEvent.change(dialog.getByRole('textbox', { name: /Reason for the stop/ }), { target: { value: ' budget reached ' } })
    fireEvent.click(stop)
    await waitFor(() => expect(bodies).toEqual([JSON.stringify({ reason: 'budget reached' })]))
    expect(await screen.findByText('Stop reason (not stored by the server): budget reached')).toBeInTheDocument()
  })

  it('a dev run needs a reason too, and does not claim a lock', () => {
    renderControls('run_X', 'running', 'dev')
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
    const dialog = within(screen.getByRole('dialog'))
    expect(dialog.getByText(/stopped dev run stays in the record/)).toBeInTheDocument()
    expect(dialog.queryByText(/cannot be run again/)).toBeNull()
    expect(dialog.getByRole('button', { name: 'Stop run' })).toBeDisabled()
  })

  it('an unknown split (the run record has not loaded) asks for a reason, as for a research run (CR-54 2)', () => {
    renderControls('run_X', 'running', undefined)
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
    const dialog = within(screen.getByRole('dialog'))
    expect(dialog.getByText(/split of this run is not known yet/)).toBeInTheDocument()
    expect(dialog.getByRole('button', { name: 'Stop run' })).toBeDisabled()
  })

  it.each(['pilot', 'exploratory', null])('a %s run stops without a reason, as before', async (split) => {
    const id = addMockRun({ runId: `run_CTRL_${split}`, kind: 'exploratory', seed: 1, rounds: 30, start: new Date().toISOString(), conditionId: null, branch: null })
    renderControls(id, 'running', split)
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
    const dialog = within(screen.getByRole('dialog'))
    expect(dialog.queryByRole('textbox')).toBeNull()
    fireEvent.click(dialog.getByRole('button', { name: 'Stop run' }))
    await waitFor(() => expect(bodies).toEqual(['{}']))
  })
})
