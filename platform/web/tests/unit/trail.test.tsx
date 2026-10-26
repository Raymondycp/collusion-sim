import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { setupServer } from 'msw/node'
import { http, HttpResponse } from 'msw'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { handlers, wsHandlers } from '@/mocks/handlers'
import { mockRuns } from '@/mocks/data'
import { trailDemo } from '@/mocks/trail'
import { trailView, type TrailLine } from '@/api/trail'
import type { EventOf } from '@/api/types'
import { TrailDrawer } from '@/features/workbench/TrailDrawer'
import { WorkbenchPage } from '@/features/workbench/WorkbenchPage'

vi.mock('lightweight-charts', () => {
  const any: unknown = new Proxy(() => any, { get: (_, k) => (k === 'then' ? undefined : any), apply: () => any })
  return { ColorType: {}, LineStyle: {}, LineType: {}, LineSeries: 'Line', HistogramSeries: 'Histogram', createSeriesMarkers: () => any, createChart: () => any }
})

const trailAsks: string[] = []
const server = setupServer(...handlers, ...wsHandlers)
server.events.on('request:start', ({ request }) => {
  const u = new URL(request.url)
  if (u.pathname.endsWith('/trail')) trailAsks.push(u.pathname)
})
beforeAll(() => server.listen({ onUnhandledFrame: 'bypass' }))
afterEach(() => {
  cleanup()
  server.resetHandlers()
  trailAsks.length = 0
})
afterAll(() => server.close())

const demo = (seq: number) => structuredClone(trailDemo.find((l) => l.seq === seq)!)
const failed = demo(4)
const talk = demo(5)
const act = demo(13)

describe('trailView: the line as the panel shows it (T9)', () => {
  it('a talk call: the request is system then user prompt, the answer is thinking then text; no tools offered (a pricing call has two)', () => {
    const v = trailView(talk)
    expect(v.messages!.map((m) => [m.role, m.parts.map((p) => p.kind)])).toEqual([
      ['request', ['system', 'user']],
      ['answer', ['thinking', 'text']],
    ])
    expect(v.messages![1]!.parts[1]).toEqual({ kind: 'text', text: 'Offer: we both hold 10.5 from now on.' })
    expect(v.tools).toEqual([])
    expect(trailView(act).tools!.map((t) => t.name)).toEqual(['set_price', 'hold'])
  })

  it('tool-call args: a string shows as it is, an object as JSON', () => {
    expect(trailView(act).messages![1]!.parts[1]).toEqual({ kind: 'tool-call', tool: 'set_price', text: '{"price": 10.5, "plan": "Keep the price."}' })
    const obj = demo(13)
    ;(obj.messages![1] as { parts: Record<string, unknown>[] }).parts[1]!.args = { price: 7 }
    expect(trailView(obj).messages![1]!.parts[1]).toEqual({ kind: 'tool-call', tool: 'set_price', text: '{\n  "price": 7\n}' })
  })

  it('a part or a message of an unknown kind keeps its raw JSON; the panel drops nothing', () => {
    const line = demo(5)
    ;(line.messages![0] as { parts: unknown[] }).parts.push({ part_kind: 'retry-prompt', content: 'Try again.' })
    line.messages!.push({ kind: 'note', text: 'x' })
    const v = trailView(line)
    expect(v.messages![0]!.parts.at(-1)).toEqual({ kind: 'unknown', partKind: 'retry-prompt', text: JSON.stringify({ part_kind: 'retry-prompt', content: 'Try again.' }, null, 2) })
    expect(v.messages!.at(-1)).toEqual({ role: 'unknown', parts: [{ kind: 'unknown', partKind: 'message note', text: JSON.stringify({ kind: 'note', text: 'x' }, null, 2) }] })
  })

  it('no cache row: no tools and no messages', () => {
    expect(trailView(failed)).toMatchObject({ tools: null, messages: null })
  })
})

function renderDrawer(line: TrailLine) {
  server.use(http.get('/api/runs/:runId/calls/:seq/trail', () => HttpResponse.json(line)))
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <TrailDrawer runId={line.run_id} seq={line.seq} onClose={() => {}} />
    </QueryClientProvider>,
  )
}

const head = async () => {
  const call = await screen.findByLabelText('Call')
  return Object.fromEntries(Array.from(call.querySelectorAll('dt')).map((dt) => [dt.textContent, dt.nextElementSibling!.textContent]))
}

describe('trail drawer (T3 to T7)', () => {
  it('the head of a call with a reply: agent, purpose, round, attempt, model, tokens, cost, latency', async () => {
    renderDrawer(talk)
    expect(await head()).toEqual({
      Call: 'A, Talk call, R1, attempt 2',
      Model: 'm-small',
      Cache: 'no hit',
      Finish: 'stop',
      Tokens: '100 in, 20 out',
      Cost: '$0.000018 (1.8e-5)',
      Latency: '2 ms',
      Settings: 'max_tokens 400, temperature 0.2',
    })
  })

  it('a nested setting shows as JSON, never as [object Object]', async () => {
    renderDrawer({ ...talk, settings: { temperature: 0.2, sent_reasoning: { effort: 'low', budget: 128 } } })
    const h = await head()
    expect(h.Settings).toBe('temperature 0.2, sent_reasoning {"effort":"low","budget":128}')
    expect(h.Settings).not.toContain('[object Object]')
  })

  it('no cache row: the text, the error fields, unknown tokens (not 0), and no request or answer', async () => {
    renderDrawer(failed)
    expect(await screen.findByText('No request or answer is stored: the call failed before a reply.')).toBeInTheDocument()
    const h = await head()
    expect(h).toMatchObject({ Tokens: 'unknown in, unknown out', Error: 'provider_error, HTTP 500, server_error', Finish: 'not recorded' })
    expect(screen.queryByRole('region', { name: 'Request' })).toBeNull()
    expect(screen.queryByRole('region', { name: 'Tools offered' })).toBeNull()
  })

  it('finish reason length gets the mark', async () => {
    renderDrawer({ ...talk, finish_reason: 'length' })
    expect((await head()).Finish).toBe('length: answer cut at the token limit')
  })

  it('a failed call with its request: the text for a missing reply', async () => {
    renderDrawer({ ...talk, missing: 'reply', messages: talk.messages!.slice(0, 1) })
    expect(await screen.findByText('The call failed: the request is stored, but there is no answer.')).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Request' })).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Answer' })).toBeNull()
  })

  it('the system prompt and the thinking start folded; the user prompt and the answer text are open', async () => {
    renderDrawer(talk)
    const req = await screen.findByRole('region', { name: 'Request' })
    const ans = screen.getByRole('region', { name: 'Answer' })
    const open = (region: HTMLElement) => Array.from(region.querySelectorAll('details')).map((d) => [d.querySelector('summary')!.textContent, d.open])
    expect(open(req)).toEqual([
      ['System prompt', false],
      ['User prompt', true],
    ])
    expect(open(ans)).toEqual([
      ['Thinking', false],
      ['Text', true],
    ])
    expect(within(ans).getByText('The rival held 10.5; holding keeps the joint price.')).toBeInTheDocument()
  })

  it('the tools offered: names and descriptions; the parameter schemas are folded', async () => {
    renderDrawer(act)
    const tools = await screen.findByRole('region', { name: 'Tools offered' })
    expect(within(tools).getByText('Tools offered (2)')).toBeInTheDocument()
    expect(within(tools).getByText('set_price')).toBeInTheDocument()
    expect(within(tools).queryByText('set_price parameters')).toBeNull()
  })

  it('a copy button copies the part text', async () => {
    const writeText = vi.fn(() => Promise.resolve())
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    renderDrawer(talk)
    fireEvent.click(await screen.findByRole('button', { name: 'Copy Text' }))
    expect(writeText).toHaveBeenCalledWith('Offer: we both hold 10.5 from now on.')
  })

  it('a 409 from the route shows its message', async () => {
    server.use(
      http.get('/api/runs/:runId/calls/:seq/trail', () =>
        HttpResponse.json({ title: 'x', status: 409, code: 'test_split_locked', message: 'Test seeds are locked until a valid rule freeze: x' }, { status: 409, headers: { 'Content-Type': 'application/problem+json' } }),
      ),
    )
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <TrailDrawer runId="run_X" seq={3} onClose={() => {}} />
      </QueryClientProvider>,
    )
    expect(await screen.findByRole('alert')).toHaveTextContent('Test seeds are locked until a valid rule freeze: x')
  })
})

describe('workbench: Trail buttons and ?call= (T1, T2, T8)', () => {
  const events = mockRuns.find((r) => r.id === 'run_MOCK01')!.events
  const lastT = Math.max(...events.map((e) => e.t))
  const actA = events.find((e): e is EventOf<'llm_call'> => e.type === 'llm_call' && e.agent_id === 'A' && e.t === lastT && e.purpose === 'act')!

  function renderPage(search: string) {
    const router = createMemoryRouter([{ path: '/workbench/:runId', element: <WorkbenchPage /> }], { initialEntries: [`/workbench/run_MOCK01${search}`] })
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )
    return router
  }

  it('the Log and the fold name the same call; a click opens the drawer and sets ?call=; Esc closes it and removes ?call=', async () => {
    const router = renderPage('')
    await screen.findAllByText(/Run ended: completed/)
    const log = screen.getByRole('list', { name: 'Event log' })
    const card = screen.getByRole('region', { name: 'Agent A' })
    expect(within(log).getByRole('button', { name: `Trail of call ${actA.seq}` })).toBeInTheDocument()
    fireEvent.click(within(card).getByRole('button', { name: `Trail of call ${actA.seq}` }))
    expect(router.state.location.search).toBe(`?call=${actA.seq}`)
    const dialog = await screen.findByRole('dialog', { name: `Trail of call ${actA.seq}` })
    expect(await within(dialog).findByText(`A, Pricing call, R${lastT}, attempt 1`)).toBeInTheDocument()
    fireEvent.keyDown(dialog, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(router.state.location.search).toBe('')
  })

  it('the drawer is not modal: no overlay, the page stays in use, another Trail button switches the call, and only Esc or Close shuts it (FE-realbackend finding 4)', async () => {
    const router = renderPage(`?call=${actA.seq}`)
    const dialog = await screen.findByRole('dialog', { name: `Trail of call ${actA.seq}` })
    await within(dialog).findByText(`A, Pricing call, R${lastT}, attempt 1`)
    expect(document.querySelector('[data-slot=dialog-overlay]')).toBeNull()
    expect(document.body.style.pointerEvents).not.toBe('none')
    // The Log behind the drawer is still reachable for assistive technology and for the pointer.
    const log = screen.getByRole('list', { name: 'Event log' })
    const other = within(log).getAllByRole('button', { name: /^Trail of call/ }).find((b) => b.getAttribute('aria-label') !== `Trail of call ${actA.seq}`)!
    fireEvent.pointerDown(log)
    fireEvent.click(log)
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    fireEvent.click(other)
    const seq = other.getAttribute('aria-label')!.replace('Trail of call ', '')
    await screen.findByRole('dialog', { name: `Trail of call ${seq}` })
    expect(router.state.location.search).toContain(`call=${seq}`)
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  describe('walk the calls, and copy one (UX review item 3)', () => {
    const all = events.flatMap((e) => (e.type === 'llm_call' ? [e.seq] : [])).sort((a, b) => a - b)
    const mid = all[10]!
    const title = () => screen.getByRole('dialog').querySelector('h2')!.textContent
    const writeText = vi.fn(() => Promise.resolve())

    it('Previous call and Next call move along the calls of the run, with the position', async () => {
      const router = renderPage(`?call=${mid}`)
      await screen.findByRole('dialog', { name: `Trail of call ${mid}` })
      // The drawer opens from ?call= before the replay has folded every event, so the position needs its own wait.
      expect(await screen.findByText(`11 of ${all.length}`)).toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: /Next call/ }))
      await screen.findByRole('dialog', { name: `Trail of call ${all[11]}` })
      expect(router.state.location.search).toBe(`?call=${all[11]}`)
      // One click, then a wait: the router renders the new ?call= after the click, and a second click before that reads the old call.
      fireEvent.click(screen.getByRole('button', { name: /Previous call/ }))
      await screen.findByRole('dialog', { name: `Trail of call ${mid}` })
      fireEvent.click(screen.getByRole('button', { name: /Previous call/ }))
      await screen.findByRole('dialog', { name: `Trail of call ${all[9]}` })
    })

    // One page render per test: each render folds the replay of 120 calls, and three in one test ran 9 s alone.
    it('Previous call is off on the first call of the run', async () => {
      renderPage(`?call=${all[0]}`)
      await screen.findByRole('dialog', { name: `Trail of call ${all[0]}` })
      expect(await screen.findByText(`1 of ${all.length}`)).toBeInTheDocument()
      expect(screen.getByRole('button', { name: /Previous call/ })).toBeDisabled()
      expect(screen.getByRole('button', { name: /Next call/ })).toBeEnabled()
    })

    it('Next call is off on the last call of the run', async () => {
      renderPage(`?call=${all.at(-1)}`)
      await screen.findByRole('dialog', { name: `Trail of call ${all.at(-1)}` })
      expect(await screen.findByText(`${all.length} of ${all.length}`)).toBeInTheDocument()
      expect(screen.getByRole('button', { name: /Next call/ })).toBeDisabled()
      expect(screen.getByRole('button', { name: /Previous call/ })).toBeEnabled()
    })

    it('the arrow keys move too, and a key in a text field does not', async () => {
      renderPage(`?call=${mid}`)
      const dialog = await screen.findByRole('dialog', { name: `Trail of call ${mid}` })
      fireEvent.keyDown(dialog, { key: 'ArrowRight' })
      await screen.findByRole('dialog', { name: `Trail of call ${all[11]}` })
      fireEvent.keyDown(screen.getByRole('dialog'), { key: 'ArrowLeft' })
      await screen.findByRole('dialog', { name: `Trail of call ${mid}` })
      const field = document.createElement('input')
      screen.getByRole('dialog').append(field)
      fireEvent.keyDown(field, { key: 'ArrowRight' })
      expect(title()).toBe(`Trail of call ${mid}`)
    })

    it('does not say Copied when the browser refuses the text (CR-92 4)', async () => {
      const refuse = vi.fn(() => Promise.reject(new Error('NotAllowedError')))
      vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText: refuse } })
      renderPage(`?call=${mid}`)
      await screen.findByRole('dialog', { name: `Trail of call ${mid}` })
      await screen.findByLabelText('Call')
      fireEvent.click(await screen.findByRole('button', { name: 'Copy call as JSON' }))
      await waitFor(() => expect(refuse).toHaveBeenCalled())
      await new Promise((r) => setTimeout(r, 20))
      expect(screen.queryByRole('button', { name: 'Copied' })).toBeNull()
    })

    it('copies the call as JSON and the metadata as text, and says so', async () => {
      vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
      renderPage(`?call=${mid}`)
      await screen.findByRole('dialog', { name: `Trail of call ${mid}` })
      await screen.findByLabelText('Call')
      fireEvent.click(await screen.findByRole('button', { name: 'Copy call as JSON' }))
      const json = JSON.parse((writeText.mock.calls[0] as unknown as [string])[0])
      expect(json).toMatchObject({ trail: 'trail.v1', run_id: 'run_MOCK01', seq: mid, missing: null })
      expect(await screen.findByRole('button', { name: 'Copied' })).toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: 'Copy metadata' }))
      const meta = (writeText.mock.calls[1] as unknown as [string])[0]
      expect(meta).toMatch(/^Call: [A-Z], (Talk|Pricing) call, R\d+, attempt 1\nModel: /)
      expect(meta).toMatch(/\nCost: \$[\d.]+ \(\d\.\de-\d\)\n/)
      expect(meta).not.toContain('[object')
    })
  })

  it('?call= on load opens that call; a second open of the same call asks the server once', async () => {
    renderPage(`?call=${actA.seq}`)
    expect(await screen.findByRole('dialog', { name: `Trail of call ${actA.seq}` })).toBeInTheDocument()
    await screen.findByText(`A, Pricing call, R${lastT}, attempt 1`)
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    fireEvent.click(within(screen.getByRole('list', { name: 'Event log' })).getByRole('button', { name: `Trail of call ${actA.seq}` }))
    await screen.findByText(`A, Pricing call, R${lastT}, attempt 1`)
    expect(trailAsks).toEqual([`/api/runs/run_MOCK01/calls/${actA.seq}/trail`])
  })

  it('public view: no Trail buttons, and ?call= opens nothing', async () => {
    renderPage(`?view=public&call=${actA.seq}`)
    await screen.findAllByText(/Run ended: completed/)
    expect(screen.queryAllByRole('button', { name: /^Trail of call/ })).toEqual([])
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(trailAsks).toEqual([])
  })

  it('only model calls get a Trail button in the Log', async () => {
    renderPage('')
    await screen.findAllByText(/Run ended: completed/)
    const log = screen.getByRole('list', { name: 'Event log' })
    for (const li of within(log).getAllByRole('listitem')) {
      const isCall = /model call/.test(li.textContent ?? '')
      expect(within(li).queryAllByRole('button', { name: /^Trail of call/ })).toHaveLength(isCall ? 1 : 0)
    }
  })
})
