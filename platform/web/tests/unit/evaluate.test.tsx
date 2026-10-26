import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import Ajv2020 from 'ajv/dist/2020'
import { load } from 'js-yaml'
import { EvaluatePage } from '@/features/evaluate/EvaluatePage'
import { handlers } from '@/mocks/handlers'
import { mockItemSet, resetMockJudge, setMockItemSets } from '@/mocks/judge'

const doc = load(readFileSync(resolve(__dirname, '../../../docs/sim/api/openapi.yaml'), 'utf8')) as { components: object }
const ajv = new Ajv2020({ strict: false, allErrors: true, validateFormats: false })
ajv.addSchema({ $id: 'c', components: doc.components })
const valid = (name: string, value: unknown) => {
  const f = ajv.compile({ $ref: `c#/components/schemas/${name}` })
  f(value)
  return f.errors ?? []
}

const server = setupServer(...handlers)
beforeAll(() => server.listen({ onUnhandledFrame: 'bypass' }))
afterAll(() => server.close())
afterEach(() => {
  cleanup()
  resetMockJudge()
})

function renderAt(path: string) {
  const router = createMemoryRouter([{ path: '/evaluations', element: <EvaluatePage /> }, { path: '/settings/research', element: <p>research files</p> }], { initialEntries: [path] })
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return router
}

const get = async (path: string) => {
  const r = await fetch(`${location.origin}/api${path}`)
  return [r.status, await r.json()] as const
}
const section = (name: string) => screen.getByRole('region', { name })
const pick = (label: string, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } })

describe('judge reads (contract 0.14.0)', () => {
  it("the mock's registries and transcripts match the contract schemas", async () => {
    for (const [path, name] of [
      ['/judge/views', 'JudgeView'],
      ['/judge/blocks', 'JudgeBlock'],
      ['/judge/item-sets', 'JudgeItemSet'],
    ] as const) {
      const [status, body] = await get(path)
      expect(status).toBe(200)
      expect((body as unknown[]).flatMap((x) => valid(name, x))).toEqual([])
    }
    expect(valid('JudgeItemSet', mockItemSet({ valid: false, problems: [{ path: 'items/3/anchors', message: 'need 6' }], id: null, items: null, paraphrases: null, jev_version: null, quote_min_words: null }))).toEqual([])
    for (const view of ['talk_only', 'talk_prices'])
      for (const transform of ['none', 'round_shuffle']) {
        const [status, body] = await get(`/runs/run_MOCK01/transcript?view=${view}&transform=${transform}`)
        expect(status).toBe(200)
        expect(valid('Transcript', body)).toEqual([])
      }
  })

  it('shows the item set with its items, anchors and paraphrase hashes, and every block with its notes', async () => {
    renderAt('/evaluations')
    const items = section('Item set')
    expect(await within(items).findByText('sha256:mockrubric01')).toBeInTheDocument()
    expect(items).toHaveTextContent('draft: wording work on pilot seeds only')
    expect(items).toHaveTextContent('jev-1.13')
    expect(within(items).getByRole('list', { name: 'Items' }).children).toHaveLength(4)
    expect(within(items).getByRole('list', { name: 'Score anchors' }).children).toHaveLength(6)
    const hashes = within(items).getByRole('table', { name: 'Paraphrase hashes' })
    const [, sets] = await get('/judge/item-sets')
    const p = sets[0].paraphrases as Record<'llm' | 'jev', { hash: string }[]>
    expect(within(hashes).getAllByRole('row').slice(1).map((r) => [...r.children].map((c) => c.textContent))).toEqual(p.llm.map((x, i) => [String(i + 1), x.hash, p.jev[i]!.hash]))
    const blocks = await within(section('Blocks')).findByRole('table', { name: 'Blocks' })
    const rows = within(blocks).getAllByRole('row').slice(1)
    expect(rows.map((r) => r.firstChild!.textContent)).toEqual([
      'Wording development (pilot seeds, disclosed)',
      'Main: every parent episode of the three case types, 3 paraphrases',
      'C3-silent: every parent episode, paraphrase 1',
      'Talk and prices view, dev half',
      'Stability repeat, 10 episodes',
      'Round shuffle, 10 episodes',
    ])
    expect(rows[4]).toHaveTextContent('no cache: calls again')
    expect(rows[5]).toHaveTextContent('transform round_shuffle')
    expect(rows[1]).toHaveTextContent('180')
    // 0.24.0 JudgeBlock.conditions: null judges every condition (DECISIONS-13 §7).
    expect(rows.map((r) => r.children[3]!.textContent)).toEqual(['all', 'C1, C2a, C3-accept', 'C3-silent', 'C1, C2a, C3-accept', 'all', 'all'])
    expect(within(hashes).getAllByRole('columnheader').at(-1)).toHaveTextContent('jev (designed, not built)')
  })

  it('CR-44 3: the page says jobs run from the CLI with the LLM judge, and that Jev is not built', async () => {
    renderAt('/evaluations')
    const intro = (await screen.findByRole('heading', { name: 'Evaluate' })).nextElementSibling!
    expect(intro).toHaveTextContent('Judge jobs run from the CLI with the LLM judge: collusion-sim evaluations estimate, start, show and resume. Jev is designed, not built.')
    expect(document.body).not.toHaveTextContent('not built yet')
    expect(document.body).not.toHaveTextContent('Both backends get the same blocks')
  })

  it('no rubric file links to Settings; a frozen one says so; an invalid one lists its problems', async () => {
    setMockItemSets([])
    let router = renderAt('/evaluations')
    fireEvent.click(await within(section('Item set')).findByRole('link', { name: 'Settings > Research files' }))
    expect(router.state.location.pathname).toBe('/settings/research')
    cleanup()

    setMockItemSets([mockItemSet({ status: 'frozen', frozen_at: '2026-10-22T09:00:00Z' })])
    renderAt('/evaluations')
    await waitFor(() => expect(section('Item set')).toHaveTextContent(/frozen at .* \(freeze stage 1\)/))
    expect(section('Item set')).not.toHaveTextContent('not valid now')
    cleanup()

    const stage = { frozen: true, frozen_at: '2026-10-22T09:00:00Z', approved_by: 'lead', valid: false, problems: ['condition:C1 changed after the freeze'] }
    server.use(http.get('/api/freeze', () => HttpResponse.json({ frozen: false, frozen_at: null, valid: false, problems: [], stages: { conditions: stage, rule: { ...stage, frozen: false } } })))
    renderAt('/evaluations')
    await waitFor(() => expect(section('Item set')).toHaveTextContent(/frozen at .* \(freeze stage 1\), not valid now: a stage 1 file changed after the freeze/))
    server.resetHandlers()
    cleanup()

    setMockItemSets([mockItemSet({ valid: false, problems: [{ path: 'items/3/anchors', message: 'need 6 anchors' }], id: null, items: null, paraphrases: null, jev_version: null, quote_min_words: null })])
    router = renderAt('/evaluations')
    const items = section('Item set')
    expect(await within(items).findByRole('alert')).toHaveTextContent('The rubric file is not valid.')
    expect(within(items).getByRole('list', { name: 'Rubric problems' })).toHaveTextContent('items/3/anchors: need 6 anchors')
    expect(within(items).queryByRole('list', { name: 'Items' })).toBeNull()
    expect(items).not.toHaveTextContent('Quote rule')
  })

  it('the preview lists completed parent runs only and shows the blinded lines, their facts and the exact text', async () => {
    const router = renderAt('/evaluations')
    const preview = section('Transcript preview')
    await waitFor(() => expect(within(preview).getByLabelText('Run').querySelectorAll('option').length).toBeGreaterThan(1))
    const ids = [...within(preview).getByLabelText('Run').querySelectorAll('option')].map((o) => (o as HTMLOptionElement).value).filter(Boolean)
    expect(ids).toContain('run_MOCK01')
    expect(ids).not.toContain('run_MOCK01PROBE')
    expect(ids).not.toContain('run_MOCK01PLACEBO')
    expect(ids).not.toContain('run_MOCK02')
    pick('Run', 'run_MOCK01')
    const lines = await within(preview).findByRole('table', { name: 'Lines' })
    const cells = within(lines).getAllByRole('row').slice(1).map((r) => [...r.children].map((c) => c.textContent))
    expect(cells.length).toBeGreaterThan(0)
    for (const [n, round, channel, sender] of cells) {
      expect(channel).toMatch(/^channel \d+$/)
      expect(sender).toMatch(/^Seller \d+$/)
      expect(Number(round)).toBeGreaterThan(0)
      expect(Number(n)).toBeGreaterThan(0)
    }
    const text = within(preview).getByLabelText('Exact text').textContent!
    expect(text).not.toMatch(/Seller [AB]\b|run_MOCK01|\bC1\b|private|public/)
    expect(text).toContain(`[1] Round ${cells[0]![1]}, ${cells[0]![2]}, ${cells[0]![3]}: ${cells[0]![4]}`)
    expect(text).not.toContain('Posted prices per round:')
    const facts = within(preview).getByLabelText('Transcript facts')
    expect(facts).toHaveTextContent(`${text.length.toLocaleString()} characters, about ${Math.ceil(text.length / 4).toLocaleString()} tokens`)
    expect(facts).toHaveTextContent('talk_only version 1, transform none')
    const hashOnly = within(facts).getByText(/^sha256:mock/).textContent

    pick('View', 'talk_prices')
    await waitFor(() => expect(within(preview).getByLabelText('Exact text')).toHaveTextContent('Posted prices per round:'))
    expect(router.state.location.search).toBe('?run=run_MOCK01&view=talk_prices')
    pick('View', 'talk_only')
    pick('Transform', 'round_shuffle')
    await waitFor(() => expect(within(preview).getByLabelText('Transcript facts')).toHaveTextContent('transform round_shuffle'))
    expect(within(within(preview).getByLabelText('Transcript facts')).getByText(/^sha256:mock/).textContent).not.toBe(hashOnly)
  })

  it('the picker says how many runs it reads, and a run outside them still opens by its link', async () => {
    let limit: string | null = null
    server.use(
      http.get('/api/runs', ({ request }) => {
        limit = new URL(request.url).searchParams.get('limit')
        return HttpResponse.json({ items: [], next_cursor: null })
      }),
    )
    renderAt('/evaluations?run=run_MOCK01')
    const preview = section('Transcript preview')
    expect(await within(preview).findByRole('table', { name: 'Lines' })).toBeTruthy()
    await waitFor(() => expect(limit).not.toBeNull())
    expect(within(preview).getByLabelText('Run')).toHaveAccessibleDescription(`From the newest ${limit} completed runs. An older run opens by its link: /evaluations?run=<run id>`)
    server.resetHandlers()
  })

  it('a branch child or an unknown run shows the server refusal', async () => {
    renderAt('/evaluations?run=run_MOCK01PROBE')
    expect(await within(section('Transcript preview')).findByRole('alert')).toHaveTextContent('A branch child is not judged')
    expect(within(section('Transcript preview')).queryByRole('table', { name: 'Lines' })).toBeNull()
    cleanup()
    renderAt('/evaluations?run=run_NOPE')
    expect(await within(section('Transcript preview')).findByRole('alert')).toHaveTextContent('Run run_NOPE was not found.')
  })
})
