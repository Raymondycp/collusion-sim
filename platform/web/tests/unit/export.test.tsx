import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { setupServer } from 'msw/node'
import { http, HttpResponse } from 'msw'
import { ApiError } from '@/api/client'
import { DEFAULT_BRANCH_PLAN, type Experiment } from '@/api/experiments'
import { experimentExports, fileName, RUN_EXPORTS, saveExport } from '@/api/exports'
import { ExperimentPage } from '@/features/experiments/ExperimentsPage'
import { ExportMenu } from '@/features/export/ExportMenu'
import { handlers } from '@/mocks/handlers'
import { resetMockExperiments } from '@/mocks/experiments'

const asked: string[] = []
const server = setupServer(...handlers)
server.events.on('request:start', ({ request }) => {
  const u = new URL(request.url)
  asked.push(u.pathname + u.search)
})
beforeAll(() => server.listen({ onUnhandledFrame: 'bypass' }))
afterAll(() => server.close())

const blobs = new Map<string, Blob>()
const saved: { name: string; href: string; blob: Blob; inDocument: boolean }[] = []
let urls = 0
beforeEach(() => {
  Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: (b: Blob) => (blobs.set(`blob:${++urls}`, b), `blob:${urls}`) })
  Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: (u: string) => void blobs.delete(u) })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
    saved.push({ name: this.download, href: this.getAttribute('href')!, blob: blobs.get(this.getAttribute('href')!)!, inDocument: this.isConnected })
  })
})
afterEach(() => {
  cleanup()
  server.resetHandlers()
  resetMockExperiments()
  blobs.clear()
  saved.length = 0
  asked.length = 0
})

const item = (items: typeof RUN_EXPORTS, label: string) => items.find((x) => x.label.startsWith(label))!

describe('run export (F3)', () => {
  it.each([
    ['Run record (episode.jsonl)', '/api/runs/run_MOCK01/episode.jsonl', 'run_MOCK01.episode.jsonl'],
    ['Run record, trail and meta (zip)', '/api/runs/run_MOCK01/export?format=zip', 'run_MOCK01.record.zip'],
    ['Run record with its branch runs, trails included (zip)', '/api/runs/run_MOCK01/export?format=zip&include=branches', 'run_MOCK01.with-branches.zip'],
    ['Prices and surplus per round (CSV)', '/api/runs/run_MOCK01/export?format=csv_rounds', 'run_MOCK01.rounds.csv'],
  ])('%s: asks %s and saves %s', async (label, url, name) => {
    await saveExport(RUN_EXPORTS.find((x) => x.label === label)!, 'run_MOCK01')
    expect(asked).toEqual([url])
    expect(saved.map((s) => s.name)).toEqual([name])
  })

  it('saves the bytes that the server sent', async () => {
    await saveExport(item(RUN_EXPORTS, 'Run record (episode.jsonl)'), 'run_MOCK01')
    const direct = await (await fetch(`${location.origin}/api/runs/run_MOCK01/episode.jsonl`)).text()
    expect(direct.split('\n').length).toBeGreaterThan(2)
    expect(await saved[0]!.blob.text()).toBe(direct)
    await saveExport(item(RUN_EXPORTS, 'Prices'), 'run_MOCK01')
    expect((await saved[1]!.blob.text()).split('\n')[0]).toBe('t,price_A,price_B,nash,joint_optimum,consumer_surplus')
  })

  it('clicks a link that is in the document, removes it, and keeps the file URL for 10 s (CR-65 3)', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    await saveExport(item(RUN_EXPORTS, 'Run record (episode.jsonl)'), 'run_MOCK01')
    expect(saved[0]!.inDocument).toBe(true)
    expect(document.querySelector('a[download]')).toBeNull()
    vi.advanceTimersByTime(9_000)
    expect(blobs.size).toBe(1)
    vi.advanceTimersByTime(1_000)
    expect(blobs.size).toBe(0)
  })

  it('the server file name wins over the default name', () => {
    const r = (cd?: string) => new Response(null, { headers: cd ? { 'Content-Disposition': cd } : {} })
    expect(fileName(r('attachment; filename="run_X.zip"'), 'other.zip')).toBe('run_X.zip')
    expect(fileName(r(), 'other.zip')).toBe('other.zip')
  })

  it('an unknown run gives the server Problem and saves nothing', async () => {
    const err = await saveExport(item(RUN_EXPORTS, 'Run record, trail and meta'), 'run_NOPE').catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).code).toBe('not_found')
    expect(saved).toEqual([])
  })

  it('the menu shows the error next to the button', async () => {
    render(<ExportMenu id="run_NOPE" items={RUN_EXPORTS} />)
    fireEvent.pointerDown(screen.getByRole('button', { name: /Export/ }), { button: 0, ctrlKey: false })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Run record (episode.jsonl)' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Run record (episode.jsonl): Run run_NOPE was not found.')
    expect(saved).toEqual([])
  })

  it.each([
    [true, 'Full record so far (includes private channels)'],
    [false, 'Full record (includes private channels)'],
  ])('live %s: the record group says %s (CR-65 4)', async (live, title) => {
    render(<ExportMenu id="run_MOCK01" items={RUN_EXPORTS} live={live} />)
    fireEvent.pointerDown(screen.getByRole('button', { name: /Export/ }), { button: 0, ctrlKey: false })
    expect(await screen.findByText(title)).toBeInTheDocument()
  })
})

describe('public view export (UX review 3, item 2)', () => {
  const open = () => fireEvent.pointerDown(screen.getByRole('button', { name: /Export/ }), { button: 0, ctrlKey: false })

  it('the notice sits beside Export in the public view only', () => {
    const { unmount } = render(<ExportMenu id="run_MOCK01" items={RUN_EXPORTS} publicView />)
    expect(screen.getByRole('note')).toHaveTextContent('the full record still contains private data')
    unmount()
    render(<ExportMenu id="run_MOCK01" items={RUN_EXPORTS} />)
    expect(screen.queryByRole('note')).toBeNull()
  })

  it('a full-record item asks first and names the private files; Cancel saves nothing, the confirm saves', async () => {
    render(<ExportMenu id="run_MOCK01" items={RUN_EXPORTS} publicView />)
    open()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Run record, trail and meta (zip)' }))
    const dialog = await screen.findByRole('dialog')
    const files = within(within(dialog).getByRole('list', { name: 'Private files' })).getAllByRole('listitem').map((x) => x.textContent)
    expect(files).toEqual([expect.stringMatching(/^episode\.jsonl .*private channel posts/), expect.stringMatching(/^trail\.jsonl .*prompts/)])
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(saved).toEqual([])

    open()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Run record, trail and meta (zip)' }))
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Export with private data' }))
    await waitFor(() => expect(saved.map((s) => s.name)).toEqual(['run_MOCK01.record.zip']))
  })

  it('the market-data CSV saves at once, with no question', async () => {
    render(<ExportMenu id="run_MOCK01" items={RUN_EXPORTS} publicView />)
    open()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Prices and surplus per round (CSV)' }))
    await waitFor(() => expect(saved.map((s) => s.name)).toEqual(['run_MOCK01.rounds.csv']))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('the god view saves a full record with no question', async () => {
    render(<ExportMenu id="run_MOCK01" items={RUN_EXPORTS} />)
    open()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Run record (episode.jsonl)' }))
    await waitFor(() => expect(saved).toHaveLength(1))
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})

async function pilotExperiment() {
  const body = {
    conditions: ['C1'],
    split: 'pilot',
    seeds: [1, 2],
    agents: ['A', 'B'].map((id) => ({ id, role: 'seller', policy: { kind: 'constant', params: { price: 5 } }, capability: null })),
    budget: { max_usd: 0.5 },
    batch_budget: { max_usd: 5 },
    branch_plan: structuredClone(DEFAULT_BRANCH_PLAN),
  }
  const r = await fetch(`${location.origin}/api/experiments`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  expect(r.status).toBe(202)
  return ((await r.json()) as Experiment).id
}

function renderExperiment(id: string) {
  const router = createMemoryRouter([{ path: '/experiments/:experimentId', element: <ExperimentPage /> }], { initialEntries: [`/experiments/${id}`] })
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

async function chooseFromMenu(name: string) {
  fireEvent.pointerDown(await screen.findByRole('button', { name: /Export/ }), { button: 0, ctrlKey: false })
  fireEvent.click(await screen.findByRole('menuitem', { name }))
}

const featureItem = (layer: string, format: string, ids: string[] = []) => experimentExports(ids).find((x) => x.label.includes(`(layer ${layer})`) && x.label.includes(`(${format}`))!

/** The mock moves each cell one step per GET of the experiment; after two GETs every parent has completed and is judged. */
async function judgedExperiment() {
  const id = await pilotExperiment()
  for (let i = 0; i < 2; i++) await fetch(`${location.origin}/api/experiments/${id}`)
  return { id, evaluationId: id.replace('exp_', 'eval_') }
}

describe('experiment export (F3)', () => {
  it.each([
    ['B', 'JSON', 'layer=B&format=json', 'features-B.json'],
    ['A', 'JSON', 'layer=A&format=json', 'features-A.json'],
  ])('layer %s %s: asks %s and saves <id>.%s', async (layer, format, query, suffix) => {
    const id = await pilotExperiment()
    asked.length = 0
    await saveExport(featureItem(layer, format), id)
    expect(asked).toEqual([`/api/experiments/${id}/features?${query}`])
    expect(saved.map((s) => s.name)).toEqual([`${id}.${suffix}`])
  })

  // analysis/p2_starter.py `load`: a CSV needs `<path>.meta.json` beside it, with the features and their code hash (CR-65 1).
  it.each(['B', 'A'])('layer %s CSV: saves the CSV and <csv>.meta.json, the pair that the P2 starter reads', async (layer) => {
    const id = await pilotExperiment()
    asked.length = 0
    await saveExport(featureItem(layer, 'CSV'), id)
    expect(asked.sort()).toEqual([`/api/experiments/${id}/features?layer=${layer}&format=csv`, `/api/experiments/${id}/features?layer=${layer}&format=json`])
    const csv = `${id}.features-${layer}.csv`
    expect(saved.map((s) => s.name)).toEqual([csv, `${csv}.meta.json`])
    const meta = JSON.parse(await saved[1]!.blob.text()) as Record<string, unknown>
    expect(meta).toMatchObject({ experiment_id: id, layer, features_code_hash: 'sha256:mockfeatures01', features: [] })
  })

  it('a failed meta call saves no half pair', async () => {
    const id = await pilotExperiment()
    server.use(http.get('/api/experiments/:id/features', ({ request }) => (new URL(request.url).searchParams.get('format') === 'json' ? HttpResponse.error() : undefined)))
    await expect(saveExport(featureItem('B', 'CSV'), id)).rejects.toBeInstanceOf(ApiError)
    expect(saved).toEqual([])
  })

  it('the experiment page saves the feature table, one row per cell', async () => {
    const id = await pilotExperiment()
    renderExperiment(id)
    await chooseFromMenu('Feature table, all features (layer B) (CSV and its .meta.json, 2 files)')
    await waitFor(() => expect(saved).toHaveLength(2))
    const lines = (await saved[0]!.blob.text()).trim().split('\n')
    expect(lines[0]).toBe('experiment_id,condition_id,split,seed,parent_run_id')
    expect(lines).toHaveLength(3)
  })

  it('the experiment page shows a 409 test_split_locked and saves nothing', async () => {
    const id = await pilotExperiment()
    server.use(
      http.get('/api/experiments/:id/features', () =>
        HttpResponse.json({ title: 'x', status: 409, code: 'test_split_locked', message: 'Rows of test seeds need a valid freeze.' }, { status: 409, headers: { 'Content-Type': 'application/problem+json' } }),
      ),
    )
    renderExperiment(id)
    await chooseFromMenu('Feature table, all features (layer B) (JSON)')
    expect(await screen.findByRole('alert')).toHaveTextContent('Rows of test seeds need a valid freeze.')
    expect(saved).toEqual([])
  })

  // Children that have not run yet show as not_judged or missing in the table (CR-65 n2).
  it.each([
    ['running', true],
    ['blocked', true],
    ['completed', false],
    ['ended', false],
  ] as const)('status %s: "so far" in the group titles is %s', async (status, sofar) => {
    const id = await pilotExperiment()
    const x = (await (await fetch(`${location.origin}/api/experiments/${id}`)).json()) as Experiment
    server.use(http.get(`/api/experiments/${id}`, () => HttpResponse.json({ ...x, status })))
    renderExperiment(id)
    fireEvent.pointerDown(await screen.findByRole('button', { name: /Export/ }), { button: 0, ctrlKey: false })
    const titles = [
      `Full record${sofar ? ' so far' : ''} (includes private channels)`,
      `Feature table as JSON${sofar ? ' so far' : ''}`,
      `Feature table as CSV${sofar ? ' so far' : ''}`,
    ]
    for (const t of titles) expect(await screen.findByText(t)).toBeInTheDocument()
  })
})

describe('experiment zip (route added in 0.26.0, E1, CR-75 2)', () => {
  const zipItem = () => experimentExports()[0]!
  const zipUrl = (id: string) => `${location.origin}/api/experiments/${id}/export?format=zip`
  const asTest = async (id: string, freeze: { valid: boolean; problems?: string[] }) => {
    const x = (await (await fetch(`${location.origin}/api/experiments/${id}`)).json()) as Experiment
    const f = await (await fetch(`${location.origin}/api/freeze`)).json()
    server.use(
      http.get(`/api/experiments/${id}`, () => HttpResponse.json({ ...x, split: 'test' })),
      http.get('/api/freeze', () => HttpResponse.json({ ...f, ...freeze })),
    )
  }

  it('the browser streams the zip from a link: no fetch of the export route, and the link is removed', async () => {
    const id = await pilotExperiment()
    asked.length = 0
    expect(await saveExport(zipItem(), id)).toEqual([`${id}.zip`])
    expect(saved).toEqual([{ name: `${id}.zip`, href: zipUrl(id), blob: undefined, inDocument: true }])
    expect(asked).toEqual([`/api/experiments/${id}`])
    expect(document.querySelector('a[download]')).toBeNull()
  })

  it('the judge ids do not change the link', async () => {
    const id = await pilotExperiment()
    await saveExport(experimentExports(['eval_1'])[0]!, id)
    expect(saved.map((s) => s.href)).toEqual([zipUrl(id)])
  })

  it('a test experiment without a valid freeze gives the 409 of the route, and no link', async () => {
    const id = await pilotExperiment()
    await asTest(id, { valid: false, problems: ['stage rule is not frozen', 'C1 changed after the freeze'] })
    asked.length = 0
    const e = await saveExport(zipItem(), id).catch((x: unknown) => x)
    expect(e).toBeInstanceOf(ApiError)
    expect(e).toMatchObject({ status: 409, code: 'test_split_locked', message: 'Test seeds are locked until a valid rule freeze: stage rule is not frozen; C1 changed after the freeze' })
    expect(asked).toEqual([`/api/experiments/${id}`, '/api/freeze'])
    expect(saved).toEqual([])
  })

  it('a test experiment with a valid freeze gets the link', async () => {
    const id = await pilotExperiment()
    await asTest(id, { valid: true, problems: [] })
    await saveExport(zipItem(), id)
    expect(saved.map((s) => s.href)).toEqual([zipUrl(id)])
  })

  it('the page shows the item in the full record group, and the 409 next to the button', async () => {
    const id = await pilotExperiment()
    await asTest(id, { valid: false, problems: ['stage rule is not frozen'] })
    renderExperiment(id)
    fireEvent.pointerDown(await screen.findByRole('button', { name: /Export/ }), { button: 0, ctrlKey: false })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Experiment record: runs, trails and manifest (zip)' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Test seeds are locked until a valid rule freeze: stage rule is not frozen')
    expect(saved).toEqual([])
  })

  it('an unknown experiment is a 404, and no link', async () => {
    await expect(saveExport(zipItem(), 'exp_01MXEX00000000000000000099')).rejects.toMatchObject({ status: 404 })
    expect(saved).toEqual([])
  })
})

describe('judge columns from a list of evaluations (0.24.0, CR-65 finding 2)', () => {
  it('a CSV item sends the evaluation ids in the CSV call and in the meta call; the pair has the judge columns and meta.judge', async () => {
    const { id, evaluationId } = await judgedExperiment()
    asked.length = 0
    const csvItem = featureItem('B', 'CSV', [evaluationId])
    expect(csvItem.label).toBe('Feature table, all features (layer B), judge columns of 1 evaluation (CSV and its .meta.json, 2 files)')
    await saveExport(csvItem, id)
    expect(asked.sort()).toEqual([
      `/api/experiments/${id}/features?layer=B&format=csv&evaluation_id=${evaluationId}`,
      `/api/experiments/${id}/features?layer=B&format=json&evaluation_id=${evaluationId}`,
    ])
    const lines = (await saved[0]!.blob.text()).trim().split('\n')
    expect(lines[0]).toBe('experiment_id,condition_id,split,seed,parent_run_id,judge_evaluation_id')
    expect(lines.slice(1).every((l) => l.endsWith(`,${evaluationId}`))).toBe(true)
    const meta = JSON.parse(await saved[1]!.blob.text()) as { judge: { evaluations: { evaluation_id: string }[] } }
    expect(meta.judge.evaluations.map((e) => e.evaluation_id)).toEqual([evaluationId])
  })

  it('several ids go in the order given, each as its own evaluation_id parameter', async () => {
    const { id } = await judgedExperiment()
    asked.length = 0
    server.use(http.get('/api/experiments/:id/features', () => HttpResponse.json({ meta: {}, columns: [], rows: [] })))
    await saveExport(featureItem('A', 'JSON', ['eval_2', 'eval_1']), id)
    expect(asked).toEqual([`/api/experiments/${id}/features?layer=A&format=json&evaluation_id=eval_2&evaluation_id=eval_1`])
  })

  it('the mock, as the server, refuses a job with no llm backend (422), which is why the page does not offer it', async () => {
    const { id } = await judgedExperiment()
    const scripted = id.replace('exp_01MXEX', 'eval_01MXSC')
    const jobs = (await (await fetch(`${location.origin}/api/evaluations?status=completed`)).json()) as { id: string }[]
    expect(jobs.map((j) => j.id)).toContain(scripted)
    await expect(saveExport(featureItem('B', 'JSON', [scripted]), id)).rejects.toMatchObject({ status: 422 })
    expect(saved).toEqual([])
  })

  it('an unknown evaluation is a 404 and saves no half pair', async () => {
    const { id } = await judgedExperiment()
    await expect(saveExport(featureItem('B', 'CSV', ['eval_01MXEX00000000000000000099']), id)).rejects.toBeInstanceOf(ApiError)
    // The table and its meta file go out together. The call fails on the first 404, and the second request may start after it. Wait for
    // both, or the late one lands in the request log of the next test.
    await waitFor(() => expect(asked.filter((u) => u.includes('evaluation_id=eval_01MXEX00000000000000000099'))).toHaveLength(2))
    expect(saved).toEqual([])
  })

  it('the experiment page lists the judge jobs of its parents; a checked job stays checked and goes into the export', async () => {
    const { id, evaluationId } = await judgedExperiment()
    renderExperiment(id)
    fireEvent.pointerDown(await screen.findByRole('button', { name: /Export/ }), { button: 0, ctrlKey: false })
    const box = await screen.findByRole('menuitemcheckbox', { name: `${evaluationId} · main · talk_only · 2 parents` })
    // The scripted-only job of the same parents is not offered: the server joins only LLM scores (CR-67 n1).
    expect(screen.getAllByRole('menuitemcheckbox')).toHaveLength(1)
    expect(screen.getByText('Judge columns from (completed judge jobs, in the order you check them)')).toBeInTheDocument()
    fireEvent.click(box)
    expect(await screen.findByRole('menuitemcheckbox', { name: new RegExp(evaluationId) })).toHaveAttribute('aria-checked', 'true')
    asked.length = 0
    fireEvent.click(screen.getByRole('menuitem', { name: 'Feature table, all features (layer B), judge columns of 1 evaluation (JSON)' }))
    await waitFor(() => expect(saved).toHaveLength(1))
    expect(asked).toContain(`/api/experiments/${id}/features?layer=B&format=json&evaluation_id=${evaluationId}`)
  })

  it('no judge job of its parents: no check boxes, and the items have no judge columns', async () => {
    await judgedExperiment()
    const id = await pilotExperiment()
    renderExperiment(id)
    fireEvent.pointerDown(await screen.findByRole('button', { name: /Export/ }), { button: 0, ctrlKey: false })
    expect(await screen.findByRole('menuitem', { name: 'Feature table, all features (layer B) (JSON)' })).toBeInTheDocument()
    expect(screen.queryByRole('menuitemcheckbox')).toBeNull()
    // The judge group stays, with the reason that it is empty, so a missing judge column is no surprise (UX review item 5).
    expect(screen.getByText('Judge columns from (completed judge jobs, in the order you check them)')).toBeInTheDocument()
    expect(screen.getByText(/No completed judge job for this experiment\. The tables then have no judge columns\./)).toBeInTheDocument()
  })
})

describe('what each export item holds (UX review item 5)', () => {
  const described = (name: string | RegExp) => {
    const item = screen.getByRole('menuitem', { name })
    const text = item.getAttribute('aria-describedby') ? document.getElementById(item.getAttribute('aria-describedby')!)?.textContent : null
    return text
  }

  it('a run menu says what each file holds, and the item names do not change', async () => {
    render(<ExportMenu id="run_MOCK01" items={RUN_EXPORTS} />)
    fireEvent.pointerDown(screen.getByRole('button', { name: /Export/ }), { button: 0, ctrlKey: false })
    expect(await screen.findByRole('menuitem', { name: 'Run record (episode.jsonl)' })).toBeInTheDocument()
    expect(described('Run record (episode.jsonl)')).toBe('One JSON line per event. The run page replays this file.')
    expect(described('Run record, trail and meta (zip)')).toBe('episode.jsonl, meta.json, trail.jsonl and manifest.json.')
    expect(described('Run record with its branch runs, trails included (zip)')).toMatch(/^The same files, and a branches\/ folder/)
    expect(described('Prices and surplus per round (CSV)')).toMatch(/^One row per round: the prices of the sellers/)
  })

  it('the feature table items say the layer, and a CSV says that it is two files and that the browser may ask', async () => {
    const id = await pilotExperiment()
    renderExperiment(id)
    fireEvent.pointerDown(await screen.findByRole('button', { name: /Export/ }), { button: 0, ctrlKey: false })
    expect(await screen.findByRole('menuitem', { name: 'Feature table, all features (layer B) (JSON)' })).toBeInTheDocument()
    expect(described('Feature table, all features (layer B) (JSON)')).toBe('Layer B: every feature, also those that read the model calls and the branch runs. One row per cell.')
    expect(described('Feature table, log only (layer A) (JSON)')).toMatch(/^Layer A: only the features that read the log\. One row per cell\.$/)
    expect(described(/layer B\) \(CSV and its \.meta\.json, 2 files\)/)).toMatch(/Two files, the \.csv and its \.meta\.json: the browser may ask to allow several downloads\.$/)
    expect(screen.getByText('The P2 starter reads this file.')).toBeInTheDocument()
    expect(screen.getByText('Opens in a spreadsheet. The P2 starter also reads the pair.')).toBeInTheDocument()
    expect(described('Experiment record: runs, trails and manifest (zip)')).toMatch(/^experiment\.json, a runs\/<run id>\/ folder for each run/)
  })
})
