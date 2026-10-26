import { api, API_BASE, ApiError, unwrap } from './client'

type Fetched = Promise<{ data?: Blob; error?: unknown; response: Response }>

/**
 * One menu item: the files that the API exports for it. `fallback` names the file when the server sends no
 * Content-Disposition (csv_rounds, features). With `meta`, the item also saves `<name>.meta.json` from that call's `meta`.
 * An `own` item always uses `fallback`, whatever the server sends: the two run zips have one server name, and the user must tell them apart.
 * A `link` item is not fetched: the browser streams the file from the URL that `link` gives after its checks.
 */
/** `about` is the line under the item: what the file holds, and when it is two files (UX review item 5). */
export type ExportItem = { label: string; group: ExportGroup; about: string; fallback: (id: string) => string; own?: true; private?: string[] } & (
  | { get: (id: string) => Fetched; meta?: (id: string) => Promise<{ data?: unknown; error?: unknown; response: Response }> }
  | { link: (id: string) => Promise<string> }
)

/** The record groups hold private channel posts, whatever view the page shows (CR-65 4). */
export type ExportGroup = 'record' | 'prices' | 'p2' | 'sheet'

const run = (run_id: string) => ({ path: { run_id } })

/** sandbox_json is in the contract, but the server answers 422 (not built), so it is not offered. */
export const RUN_EXPORTS: ExportItem[] = [
  {
    label: 'Run record (episode.jsonl)',
    group: 'record',
    about: 'One JSON line per event. The run page replays this file.',
    private: ['episode.jsonl (private channel posts and what the sellers saw)'],
    get: (id) => api.GET('/runs/{run_id}/episode.jsonl', { params: run(id), parseAs: 'blob' }),
    fallback: (id) => `${id}.episode.jsonl`,
  },
  {
    label: 'Run record, trail and meta (zip)',
    group: 'record',
    about: 'episode.jsonl, meta.json, trail.jsonl and manifest.json.',
    private: ['episode.jsonl (private channel posts and what the sellers saw)', 'trail.jsonl (the prompts and replies of the model calls)'],
    get: (id) => api.GET('/runs/{run_id}/export', { params: { ...run(id), query: { format: 'zip' } }, parseAs: 'blob' }),
    fallback: (id) => `${id}.record.zip`,
    own: true,
  },
  {
    label: 'Run record with its branch runs, trails included (zip)',
    group: 'record',
    about: 'The same files, and a branches/ folder with the same files for each branch child.',
    private: ['episode.jsonl (private channel posts and what the sellers saw)', 'trail.jsonl (the prompts and replies of the model calls)', 'the same two files in branches/'],
    get: (id) => api.GET('/runs/{run_id}/export', { params: { ...run(id), query: { format: 'zip', include: 'branches' } }, parseAs: 'blob' }),
    fallback: (id) => `${id}.with-branches.zip`,
    own: true,
  },
  {
    label: 'Prices and surplus per round (CSV)',
    group: 'prices',
    about: 'One row per round: the prices of the sellers, the Nash price, the joint optimum and the consumer surplus.',
    get: (id) => api.GET('/runs/{run_id}/export', { params: { ...run(id), query: { format: 'csv_rounds' } }, parseAs: 'blob' }),
    fallback: (id) => `${id}.rounds.csv`,
  },
]

const table = (experiment_id: string, layer: 'A' | 'B', format: 'csv' | 'json', evaluationIds: string[]) => ({
  params: { path: { experiment_id }, query: { layer, format, ...(evaluationIds.length ? { evaluation_id: evaluationIds } : {}) } },
})
const LAYER = { B: 'all features (layer B)', A: 'log only (layer A)' }
const judged = (ids: string[]) => (ids.length ? `, judge columns of ${ids.length} evaluation${ids.length > 1 ? 's' : ''}` : '')

/**
 * The P2 starter reads the JSON table, or a CSV with `<csv>.meta.json` beside it (analysis/p2_starter.py `load`), so a CSV
 * item saves that pair, as `collusion-sim features export --format csv` does (CR-65 1). The meta comes from a second
 * call; it names the features and their code hash, which do not change between the two calls.
 */
const features = (layer: 'A' | 'B', format: 'csv' | 'json', ids: string[]): ExportItem => ({
  label: format === 'json' ? `Feature table, ${LAYER[layer]}${judged(ids)} (JSON)` : `Feature table, ${LAYER[layer]}${judged(ids)} (CSV and its .meta.json, 2 files)`,
  group: format === 'json' ? 'p2' : 'sheet',
  about:
    (layer === 'B' ? 'Layer B: every feature, also those that read the model calls and the branch runs. ' : 'Layer A: only the features that read the log. ') +
    (format === 'json' ? 'One row per cell.' : 'One row per cell. Two files, the .csv and its .meta.json: the browser may ask to allow several downloads.'),
  get: (id) => api.GET('/experiments/{experiment_id}/features', { ...table(id, layer, format, ids), parseAs: 'blob' }),
  fallback: (id) => `${id}.features-${layer}.${format}`,
  meta: format === 'csv' ? (id) => api.GET('/experiments/{experiment_id}/features', table(id, layer, 'json', ids)) : undefined,
})

/**
 * Added in 0.26.0: experiment.json, then runs/<run_id>/ (episode.jsonl, meta.json, trail.jsonl) for each started run, then
 * manifest.json. The zip can be large, so the browser streams it (CR-75 2). A link cannot show a Problem, and the route has
 * no HEAD, so the item first asks the gates of the route: the experiment (404), and for a test experiment the freeze (409).
 */
const experimentZip: ExportItem = {
  label: 'Experiment record: runs, trails and manifest (zip)',
  group: 'record',
  about: 'experiment.json, a runs/<run id>/ folder for each run (episode.jsonl, meta.json, trail.jsonl) and manifest.json.',
  link: async (id) => {
    const x = await unwrap(api.GET('/experiments/{experiment_id}', { params: { path: { experiment_id: id } } }))
    if (x.split === 'test') {
      const f = await unwrap(api.GET('/freeze'))
      if (!f.valid) throw new ApiError(409, 'test_split_locked', `Test seeds are locked until a valid rule freeze: ${(f.problems ?? []).join('; ')}`)
    }
    return `${location.origin}${API_BASE}/experiments/${encodeURIComponent(id)}/export?format=zip`
  },
  fallback: (id) => `${id}.zip`,
}

/**
 * The experiment zip, then the feature table items. With evaluation ids, the CSV, the JSON and the meta call all send them, so the table has the
 * judge columns and `meta.judge` names the evaluations (0.24.0; CR-65 finding 2).
 */
export const experimentExports = (evaluationIds: string[] = []): ExportItem[] => [
  experimentZip,
  features('B', 'json', evaluationIds),
  features('A', 'json', evaluationIds),
  features('B', 'csv', evaluationIds),
  features('A', 'csv', evaluationIds),
]

export function fileName(response: Response, fallback: string): string {
  const m = /filename="?([^";]+)"?/.exec(response.headers.get('Content-Disposition') ?? '')
  return m?.[1] ?? fallback
}

function click(href: string, name: string) {
  const a = document.createElement('a')
  a.href = href
  a.download = name
  document.body.append(a)
  a.click()
  a.remove()
}

/** The anchor is in the document for the click, and the URL lives 10 s, so Firefox and Safari read the blob before the revoke (CR-65 3). */
function saveBlob(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob)
  click(url, name)
  setTimeout(() => URL.revokeObjectURL(url), 10_000)
}

/**
 * Fetches every file of the item, then saves them, so an error saves nothing. A Problem from the server (404,
 * 409 test_split_locked, 422) comes back as an ApiError. Returns the saved file names.
 */
export async function saveExport(item: ExportItem, id: string): Promise<string[]> {
  if ('link' in item) {
    click(await item.link(id), item.fallback(id))
    return [item.fallback(id)]
  }
  const p = item.get(id)
  const [blob, meta] = await Promise.all([unwrap(p), item.meta ? unwrap(item.meta(id)).then((t) => (t as { meta: unknown }).meta) : undefined])
  const name = item.own ? item.fallback(id) : fileName((await p).response, item.fallback(id))
  saveBlob(blob, name)
  if (meta === undefined) return [name]
  saveBlob(new Blob([JSON.stringify(meta, null, 2) + '\n'], { type: 'application/json' }), `${name}.meta.json`)
  return [name, `${name}.meta.json`]
}
