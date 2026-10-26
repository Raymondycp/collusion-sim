import type { ReactNode } from 'react'
import { Link, useSearchParams } from 'react-router'
import { TRANSFORMS, useJudgeBlocks, useJudgeItemSets, useJudgeViews, useTranscript, type JudgeItemSet, type TransformId, type ViewId } from '@/api/judge'
import { RUNS_LIMIT, useRuns } from '@/api/queries'
import { useFreezePreview, type StageStatus } from '@/api/settings'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { NativeSelect } from '@/features/new-run/SchemaField'

function Alert({ children }: { children: ReactNode }) {
  return (
    <p role="alert" className="text-sm text-red-600 dark:text-red-400">
      {children}
    </p>
  )
}

const Mono = ({ children }: { children: ReactNode }) => <span className="font-mono text-xs break-all">{children}</span>

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section aria-label={title} className="space-y-2 rounded-lg border p-3">
      <h2 className="font-medium">{title}</h2>
      {children}
    </section>
  )
}

/** The rubric both backends use. The UI never edits it here: Settings > Research files holds the file. */
function ItemSetView({ s, stage }: { s: JudgeItemSet; stage: StageStatus | undefined }) {
  return (
    <div className="space-y-2 text-sm">
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
        <dt className="text-muted-foreground">File</dt>
        <dd>
          <Mono>{s.file}</Mono>
        </dd>
        <dt className="text-muted-foreground">Hash</dt>
        <dd>
          <Mono>{s.hash}</Mono>
        </dd>
        <dt className="text-muted-foreground">Status</dt>
        <dd>
          {s.status === 'frozen'
            ? `frozen${s.frozen_at ? ` at ${new Date(s.frozen_at).toLocaleString()}` : ''} (freeze stage 1)${stage?.frozen && !stage.valid ? ', not valid now: a stage 1 file changed after the freeze. See Settings > Freeze' : ''}`
            : 'draft: wording work on pilot seeds only'}
        </dd>
        {s.valid && (
          <>
            <dt className="text-muted-foreground">Id</dt>
            <dd>{s.id}</dd>
            <dt className="text-muted-foreground">Jev model (designed, not built)</dt>
            <dd>{s.jev_version}</dd>
            <dt className="text-muted-foreground">Quote rule</dt>
            <dd>A quote shorter than {s.quote_min_words} words must be the whole message.</dd>
          </>
        )}
      </dl>
      {!s.valid && (
        <>
          <Alert>The rubric file is not valid. No judge job can use it.</Alert>
          <ul aria-label="Rubric problems" className="ml-4 list-disc text-red-700 dark:text-red-400">
            {s.problems.map((p) => (
              <li key={`${p.path}:${p.message}`}>
                {p.path}: {p.message}
              </li>
            ))}
          </ul>
        </>
      )}
      {s.guard && <p>Guard: {s.guard}</p>}
      {s.items && (
        <ol aria-label="Items" className="space-y-1">
          {s.items.map((it) => (
            <li key={it.id} className="rounded-md border p-2">
              <Mono>{it.id}</Mono> {it.text}
              {it.anchors && (
                <ol aria-label="Score anchors" className="mt-1 ml-4 text-xs text-muted-foreground">
                  {it.anchors.map((a, i) => (
                    <li key={i}>
                      {i}: {a}
                    </li>
                  ))}
                </ol>
              )}
            </li>
          ))}
        </ol>
      )}
      {s.paraphrases && (
        <table aria-label="Paraphrase hashes" className="w-full text-left">
          <thead className="text-xs text-muted-foreground">
            <tr>
              <th className="py-1 pr-3 font-normal">Paraphrase</th>
              <th className="py-1 pr-3 font-normal">llm</th>
              <th className="py-1 font-normal">jev (designed, not built)</th>
            </tr>
          </thead>
          <tbody>
            {s.paraphrases.llm.map((p, i) => (
              <tr key={p.index} className="border-t align-top">
                <td className="py-1 pr-3">{p.index}</td>
                <td className="py-1 pr-3">
                  <Mono>{p.hash}</Mono>
                </td>
                <td className="py-1">
                  <Mono>{s.paraphrases!.jev[i]?.hash}</Mono>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

function ItemSets() {
  const q = useJudgeItemSets()
  const freeze = useFreezePreview()
  return (
    <Section title="Item set">
      {q.isPending && <Skeleton className="h-24 w-full" />}
      {q.error && <Alert>{q.error.message}</Alert>}
      {q.data?.length === 0 && (
        <p className="text-sm text-muted-foreground">
          No judge rubric yet. Write conditions/judge-rubric.yaml in <Link to="/settings/research" className="underline">Settings &gt; Research files</Link>.
        </p>
      )}
      {q.data?.map((s) => (
        <ItemSetView key={s.file} s={s} stage={freeze.data?.stages.conditions} />
      ))}
    </Section>
  )
}

function Blocks() {
  const q = useJudgeBlocks()
  return (
    <Section title="Blocks">
      <p className="text-xs text-muted-foreground">A block fixes its splits, conditions, views and paraphrases. A parent of another condition is skipped, not judged. Caps are calls per backend. Only the LLM judge runs. Jev is designed, not built.</p>
      {q.isPending && <Skeleton className="h-24 w-full" />}
      {q.error && <Alert>{q.error.message}</Alert>}
      {q.data && (
        <div className="overflow-x-auto">
          <table aria-label="Blocks" className="w-full text-left text-sm">
            <thead className="text-xs text-muted-foreground">
              <tr>
                {['Block', 'Tag', 'Splits', 'Conditions', 'Views', 'Paraphrases', 'Cap', 'Notes'].map((h) => (
                  <th key={h} className="py-1 pr-3 font-normal">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {q.data.map((b) => (
                <tr key={b.id} className="border-t align-top">
                  <td className="py-1 pr-3">{b.name}</td>
                  <td className="py-1 pr-3">{b.tag}</td>
                  <td className="py-1 pr-3">{b.splits.join(', ')}</td>
                  <td className="py-1 pr-3">{b.conditions?.join(', ') ?? 'all'}</td>
                  <td className="py-1 pr-3">{b.views.join(', ')}</td>
                  <td className="py-1 pr-3">{b.paraphrases.join(', ')}</td>
                  <td className="py-1 pr-3">{b.cap_per_backend}</td>
                  <td className="py-1">{[b.transform !== 'none' && `transform ${b.transform}`, !b.cache && 'no cache: calls again'].filter(Boolean).join('; ')}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  )
}

/** The exact bytes a backend gets: neutral names only. The run picker shows the researcher's fields; the transcript never does. */
function Preview() {
  const [params, setParams] = useSearchParams()
  const runId = params.get('run')
  const view = (params.get('view') ?? 'talk_only') as ViewId
  const transform = (params.get('transform') ?? 'none') as TransformId
  const set = (k: string, v: string) =>
    setParams(
      (p) => {
        const n = new URLSearchParams(p)
        if (v) n.set(k, v)
        else n.delete(k)
        return n
      },
      { replace: true },
    )
  const views = useJudgeViews()
  const runs = useRuns({ status: 'completed' })
  const parents = runs.data?.items.filter((r) => !r.branch) ?? []
  const t = useTranscript(runId, view, transform)
  return (
    <Section title="Transcript preview">
      <p className="text-xs text-muted-foreground">Completed parent runs only: a branch child is not judged.</p>
      <div className="grid gap-3 sm:grid-cols-3">
        <div className="space-y-1">
          <Label htmlFor="tp-run">Run</Label>
          <NativeSelect id="tp-run" aria-describedby="tp-run-note" value={runId ?? ''} onChange={(e) => set('run', e.target.value)}>
            <option value="">Choose a run</option>
            {parents.map((r) => (
              <option key={r.id} value={r.id}>
                {r.name ?? r.id} ({[r.kind, r.condition_id, r.split, `seed ${r.seed}`].filter(Boolean).join(', ')})
              </option>
            ))}
          </NativeSelect>
          <p id="tp-run-note" className="text-xs text-muted-foreground">
            From the newest {RUNS_LIMIT} completed runs. An older run opens by its link: <Mono>/evaluations?run=&lt;run id&gt;</Mono>
          </p>
        </div>
        <div className="space-y-1">
          <Label htmlFor="tp-view">View</Label>
          <NativeSelect id="tp-view" value={view} onChange={(e) => set('view', e.target.value)}>
            {(views.data ?? []).map((v) => (
              <option key={v.id} value={v.id}>
                {v.name} (version {v.version})
              </option>
            ))}
          </NativeSelect>
        </div>
        <div className="space-y-1">
          <Label htmlFor="tp-transform">Transform</Label>
          <NativeSelect id="tp-transform" value={transform} onChange={(e) => set('transform', e.target.value)}>
            {TRANSFORMS.map((x) => (
              <option key={x.id} value={x.id}>
                {x.name}
              </option>
            ))}
          </NativeSelect>
        </div>
      </div>
      {runs.error && <Alert>{runs.error.message}</Alert>}
      {runs.data && parents.length === 0 && <p className="text-sm text-muted-foreground">No completed parent runs yet.</p>}
      {views.data && <p className="text-xs text-muted-foreground">{views.data.find((v) => v.id === view)?.description}</p>}
      {t.isFetching && !t.data && <Skeleton className="h-32 w-full" />}
      {t.error && <Alert>{t.error.message}</Alert>}
      {t.data && (
        <div className="space-y-2">
          <dl aria-label="Transcript facts" className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
            <dt className="text-muted-foreground">Input hash</dt>
            <dd>
              <Mono>{t.data.input_hash}</Mono>
            </dd>
            <dt className="text-muted-foreground">View</dt>
            <dd>
              {t.data.view} version {t.data.view_version}, transform {t.data.transform}
            </dd>
            <dt className="text-muted-foreground">Size</dt>
            <dd>
              {t.data.chars.toLocaleString()} characters, about {t.data.tokens_est.toLocaleString()} tokens
            </dd>
          </dl>
          <p className="text-xs text-muted-foreground">Both backends get these same bytes. The round is the delivery round, which the quote check uses.</p>
          {t.data.lines.length === 0 ? (
            <p className="text-sm text-muted-foreground">No messages were posted.</p>
          ) : (
            <div className="overflow-x-auto">
              <table aria-label="Lines" className="w-full text-left text-sm">
                <thead className="text-xs text-muted-foreground">
                  <tr>
                    {['#', 'Round', 'Channel', 'Sender', 'Text'].map((h) => (
                      <th key={h} className="py-1 pr-3 font-normal">
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {t.data.lines.map((l) => (
                    <tr key={l.n} className="border-t align-top">
                      <td className="py-1 pr-3">{l.n}</td>
                      <td className="py-1 pr-3">{l.round}</td>
                      <td className="py-1 pr-3 whitespace-nowrap">{l.channel}</td>
                      <td className="py-1 pr-3 whitespace-nowrap">{l.sender}</td>
                      <td className="py-1">{l.text}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <details>
            <summary className="cursor-pointer text-sm">Exact text the backends get</summary>
            <pre aria-label="Exact text" className="mt-2 max-h-96 overflow-auto rounded-md border p-2 text-xs whitespace-pre-wrap">
              {t.data.text}
            </pre>
          </details>
        </div>
      )}
    </Section>
  )
}

export function EvaluatePage() {
  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">Evaluate</h1>
      <p className="text-sm text-muted-foreground">
        The judge reads the conversation only and gives features, never a verdict. This page shows the rubric, the blocks and the blinded transcript a judge reads. Judge jobs run from the CLI with the LLM judge: <code>collusion-sim evaluations estimate</code>, <code>start</code>, <code>show</code> and <code>resume</code>. Jev is designed, not built.
      </p>
      <ItemSets />
      <Blocks />
      <Preview />
    </div>
  )
}
