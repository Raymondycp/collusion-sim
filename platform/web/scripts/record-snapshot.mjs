// Records a read-only snapshot of the API for the web UI's `snapshot` mode (FE-snapshot-brief, lead 26 Oct).
// GET requests only; the request set is the one the UI sends (docs/sim/review/FE-snapshot-report.md).
// Usage: node scripts/record-snapshot.mjs [--base http://127.0.0.1:8018] [--out snapshot] [--runs-dir <study>/runs] [--shared-only] [exp_id ...]
// --shared-only records index.json again and keeps the run files.
// --runs-dir reads each call trail from the run folder's trail.jsonl, read only. The API answers with that same line
// (src/services/read.py trail_line), and one trail request can take seconds on a busy server.
// Output: <out>/index.json (key -> {status, body}) and <out>/runs/<run_id>.json.gz (the run's keys plus its stored events).
import fs from 'node:fs'
import path from 'node:path'
import zlib from 'node:zlib'

const DEFAULT_EXPERIMENTS = [
  'exp_01M5S0FH2S614XMN0Q0YBKG5TT', // dev2
  'exp_01M5RYA4EYY99257425HF40V9P', // dev2 PC-plus
  'exp_01M5SM67A2DA6CHFMA5BM2HV2J', // test
]
// The workbench asks for these indicator instances (recorded from the UI, 26 Oct).
const INDICATORS = [
  ['br_gap_net', { baseline: 0 }],
  ['corr_cond', { window: 8 }],
  ['corr_lag', { window: 8 }],
  ['corr_raw', { window: 8 }],
  ['delta_p', { smooth: 1 }],
  ['msg_volume', {}],
]
const CONCURRENCY = 6

const argv = process.argv.slice(2)
const opt = (name, fallback) => {
  const i = argv.indexOf(name)
  if (i < 0) return fallback
  const v = argv[i + 1]
  argv.splice(i, 2)
  return v
}
const BASE = opt('--base', 'http://127.0.0.1:8018')
const OUT = path.resolve(opt('--out', path.join(import.meta.dirname, '..', 'snapshot')))
const RUNS_DIR = opt('--runs-dir', null)
const SHARED_ONLY = argv.includes('--shared-only')
if (SHARED_ONLY) argv.splice(argv.indexOf('--shared-only'), 1)
const EXPERIMENTS = argv.length ? argv : DEFAULT_EXPERIMENTS

/** The lookup key of a request: method, path and sorted query. The snapshot handlers build the same key. */
function keyOf(method, pathname, search = '') {
  const q = new URLSearchParams(search)
  q.sort()
  const s = q.toString()
  return `${method} ${pathname}${s ? `?${s}` : ''}`
}

async function get(pathAndQuery) {
  const res = await fetch(BASE + pathAndQuery)
  const text = await res.text()
  let body
  try {
    body = JSON.parse(text)
  } catch {
    throw new Error(`${pathAndQuery}: answer is not JSON (${res.status})`)
  }
  return { status: res.status, body }
}

async function record(store, pathAndQuery, transform = (b) => b) {
  const u = new URL(BASE + pathAndQuery)
  const r = await get(pathAndQuery)
  store[keyOf('GET', u.pathname, u.search)] = { status: r.status, body: r.status === 200 ? transform(r.body) : r.body }
  return r
}

async function allRuns(query) {
  const items = []
  let cursor = null
  do {
    const q = new URLSearchParams(query)
    q.set('limit', '200')
    if (cursor) q.set('cursor', cursor)
    const { body } = await get(`/api/runs?${q}`)
    items.push(...body.items)
    cursor = body.next_cursor
  } while (cursor)
  return items
}

/** Every stored event of a run, as the server streams them over the WebSocket from seq 0. */
function events(runId) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(`${BASE.replace(/^http/, 'ws')}/api/ws/runs/${runId}?from_seq=0`)
    const out = []
    let caught = false
    const timer = setTimeout(() => {
      ws.close()
      reject(new Error(`${runId}: no caught_up within 60 s`))
    }, 60_000)
    ws.onmessage = (e) => {
      const m = JSON.parse(e.data)
      if (m.type === 'event') out.push(m.event)
      if (m.type === 'caught_up') {
        caught = true
        clearTimeout(timer)
        ws.close()
      }
    }
    ws.onclose = () => (caught ? resolve(out) : reject(new Error(`${runId}: closed before caught_up`)))
    ws.onerror = () => reject(new Error(`${runId}: WebSocket error`))
  })
}

async function pool(items, fn) {
  let i = 0
  await Promise.all(Array.from({ length: CONCURRENCY }, async () => {
    while (i < items.length) await fn(items[i++])
  }))
}

async function main() {
  const t0 = Date.now()
  const health = await get('/api/health')
  if (health.status !== 200) throw new Error(`no server at ${BASE}`)
  if (!SHARED_ONLY) fs.rmSync(OUT, { recursive: true, force: true })
  fs.mkdirSync(path.join(OUT, 'runs'), { recursive: true })
  const index = {}

  const experiments = {}
  for (const id of EXPERIMENTS) {
    experiments[id] = (await record(index, `/api/experiments/${id}`)).body
    // The workbench metrics panel reads the run's row of its experiment's feature table (src/features/workbench/metrics.ts).
    await record(index, `/api/experiments/${id}/features?layer=B`)
  }
  const runIds = new Set()
  for (const x of Object.values(experiments))
    for (const c of x.cells) for (const r of [c.parent, ...c.branches]) if (r?.run_id) runIds.add(r.run_id)

  const inScope = (rows) => rows.filter((r) => runIds.has(r.id))
  index[keyOf('GET', '/api/runs', 'limit=200')] = { status: 200, body: { items: inScope(await allRuns({})), next_cursor: null } }
  index[keyOf('GET', '/api/runs', 'status=completed&limit=200')] = { status: 200, body: { items: inScope(await allRuns({ status: 'completed' })), next_cursor: null } }
  await record(index, '/api/experiments', (rows) => rows.filter((x) => EXPERIMENTS.includes(x.id)))

  for (const p of ['/api/health', '/api/indicators', '/api/scenarios', '/api/presets', '/api/seeds', '/api/freeze', '/api/conditions',
    '/api/evaluations?status=completed', '/api/judge/item-sets', '/api/judge/blocks', '/api/judge/views', '/api/demand-models'])
    await record(index, p)
  for (const c of index[keyOf('GET', '/api/conditions')].body) await record(index, `/api/conditions/${encodeURIComponent(c.id)}`)

  const runs = index[keyOf('GET', '/api/runs', 'limit=200')].body.items
  const missing = [...runIds].filter((id) => !runs.some((r) => r.id === id))
  if (missing.length) throw new Error(`runs not in GET /runs: ${missing.join(', ')}`)
  for (const s of new Set(runs.map((r) => r.scenario_id).filter(Boolean))) {
    await record(index, `/api/scenarios/${s}`)
    await record(index, `/api/scenarios/${s}/reference`)
  }

  let done = 0
  await pool(SHARED_ONLY ? [] : [...runIds], async (id) => {
    const store = {}
    await record(store, `/api/runs/${id}`)
    await record(store, `/api/runs/${id}/branches`)
    for (const [ind, params] of INDICATORS) await record(store, `/api/runs/${id}/indicators/${ind}?${new URLSearchParams({ params: JSON.stringify(params) })}`)
    const evs = await events(id)
    const stored = RUNS_DIR && path.join(RUNS_DIR, id, 'trail.jsonl')
    if (stored && fs.existsSync(stored)) {
      const lines = new Map(fs.readFileSync(stored, 'utf8').split('\n').filter((l) => l.trim()).map((l) => JSON.parse(l)).map((l) => [l.seq, l]))
      for (const e of evs) {
        if (e.type !== 'llm_call') continue
        const line = lines.get(e.seq)
        store[keyOf('GET', `/api/runs/${id}/calls/${e.seq}/trail`)] = line
          ? { status: 200, body: line }
          : { status: 404, body: { title: 'not found', status: 404, code: 'not_found', message: `Run ${id} has no llm_call with seq ${e.seq}.` } }
      }
    } else for (const e of evs) if (e.type === 'llm_call') await record(store, `/api/runs/${id}/calls/${e.seq}/trail`)
    fs.writeFileSync(path.join(OUT, 'runs', `${id}.json.gz`), zlib.gzipSync(JSON.stringify({ responses: store, events: evs }), { level: 9 }))
    done++
    console.log(`${done}/${runIds.size} ${id} ${evs.length} events`)
  })

  fs.writeFileSync(path.join(OUT, 'index.json'), JSON.stringify({
    recorded_at: new Date().toISOString(),
    source: BASE,
    api_version: health.body.api_version,
    experiments: EXPERIMENTS,
    runs: [...runIds].sort(),
    responses: index,
  }))
  console.log(`${runIds.size} runs, ${Object.keys(index).length} shared responses -> ${OUT} (${Math.round((Date.now() - t0) / 1000)} s)`)
}

main().catch((e) => {
  console.error(e.message)
  process.exit(1)
})
