import type { ReactNode } from 'react'
import { Link, useParams } from 'react-router'
import { ArrowLeft } from 'lucide-react'
import { changedSinceFreeze, useCondition, useConditions, useFreeze, useSeeds, variantSeedRule, type Condition } from '@/api/runConfig'
import { Skeleton } from '@/components/ui/skeleton'
import { readCallsText } from './limits'

function Alert({ children }: { children: ReactNode }) {
  return (
    <p role="alert" className="text-sm text-red-600 dark:text-red-400">
      {children}
    </p>
  )
}

const Mono = ({ children }: { children: ReactNode }) => <span className="font-mono text-xs break-all">{children}</span>

export function ConditionStatus({ c }: { c: Pick<Condition, 'status' | 'frozen_hash' | 'hash'> }) {
  return (
    <span className="inline-flex flex-wrap gap-1">
      <span className={c.status === 'frozen' ? 'rounded border px-1.5 text-xs' : 'rounded border border-dashed px-1.5 text-xs text-muted-foreground'}>{c.status}</span>
      {changedSinceFreeze(c) && <span className="rounded border border-red-500/50 px-1.5 text-xs text-red-600 dark:text-red-400">changed since the freeze</span>}
    </span>
  )
}

/** Seeds per split and the freeze state. Read-only: the seed map and the freeze gate live on the server. */
function SeedsAndFreeze() {
  const seeds = useSeeds()
  const freeze = useFreeze()
  return (
    <section className="space-y-2 rounded-lg border p-3">
      <h2 className="font-medium">Seeds and freeze</h2>
      {seeds.error && <Alert>{seeds.error.message}</Alert>}
      {seeds.data && (
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
          {(['pilot', 'dev', 'test'] as const).map((k) => (
            <div key={k} className="contents">
              <dt className="text-muted-foreground">{k}</dt>
              <dd>
                <Mono>{seeds.data[k].join(', ')}</Mono>
                {k === 'test' && seeds.data.test_locked && <span className="ml-2 text-xs text-muted-foreground">locked until the freeze</span>}
              </dd>
            </div>
          ))}
          <dt className="text-muted-foreground">Seed map hash</dt>
          <dd>
            <Mono>{seeds.data.hash}</Mono>
          </dd>
        </dl>
      )}
      {freeze.error && <Alert>{freeze.error.message}</Alert>}
      {freeze.data && (
        <div className="text-sm">
          <p>Freeze: {freeze.data.frozen ? `frozen at ${freeze.data.frozen_at ? new Date(freeze.data.frozen_at).toLocaleString() : 'unknown time'}, ${freeze.data.valid ? 'valid' : 'not valid'}` : 'not frozen'}</p>
          {freeze.data.problems?.length ? (
            <ul aria-label="Freeze problems" className="ml-4 list-disc text-muted-foreground">
              {freeze.data.problems.map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
          ) : null}
        </div>
      )}
      <p className="text-xs text-muted-foreground">Research runs start on pilot seeds. Dev seeds need a frozen condition. Test seeds open only after a valid freeze.</p>
    </section>
  )
}

export function ConditionsPage() {
  const list = useConditions()
  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">Conditions</h1>
      <p className="text-sm text-muted-foreground">Each condition sets the labels, channels, call mode, tools and prompts of its research runs. They are read-only here; the files are in conditions/ on the server.</p>
      {list.isPending && <Skeleton className="h-32 w-full" />}
      {list.error && <Alert>{list.error.message}</Alert>}
      {list.data && (
        <div className="overflow-x-auto rounded-lg border">
          <table className="w-full text-left text-sm">
            <thead className="bg-muted/50 text-xs text-muted-foreground">
              <tr>
                <th className="p-2">Condition</th>
                <th className="p-2">Group</th>
                <th className="p-2">Legal</th>
                <th className="p-2">Harm</th>
                <th className="p-2">Status</th>
                <th className="p-2">Tag</th>
                <th className="p-2">Hash</th>
              </tr>
            </thead>
            <tbody>
              {list.data.map((c) => (
                <tr key={c.id} className="border-t">
                  <td className="p-2">
                    <Link to={`/conditions/${c.id}`} className="font-medium underline-offset-2 hover:underline">
                      {c.id}
                    </Link>
                    <div className="text-xs text-muted-foreground">{c.name}</div>
                  </td>
                  <td className="p-2">{c.group}</td>
                  <td className="p-2">{c.labels.legal}</td>
                  <td className="p-2">{c.labels.harm}</td>
                  <td className="p-2">
                    <ConditionStatus c={c} />
                  </td>
                  <td className="p-2 text-xs">{c.tag}</td>
                  <td className="max-w-40 truncate p-2 font-mono text-xs" title={c.hash}>
                    {c.hash}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <SeedsAndFreeze />
    </div>
  )
}

const onOff = (v: boolean | undefined) => (v ? 'on' : 'off')
const sameModel = (v: boolean | null | undefined) => (v == null ? 'free' : v ? 'the same model for every seller' : 'a different model family for each seller')

export function ConditionPage() {
  const { conditionId = '' } = useParams()
  const q = useCondition(conditionId)
  const c = q.data
  return (
    <div className="max-w-4xl space-y-4">
      <Link to="/conditions" className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeft className="size-4" /> Conditions
      </Link>
      {q.isPending && <Skeleton className="h-64 w-full" />}
      {q.error && <Alert>{q.error.message}</Alert>}
      {c && (
        <>
          <div className="space-y-1">
            <h1 className="flex flex-wrap items-center gap-2 text-lg font-semibold">
              {c.id}: {c.name} <ConditionStatus c={c} />
            </h1>
            <p className="text-sm text-muted-foreground">{c.description}</p>
          </div>
          {changedSinceFreeze(c) && (
            <p role="alert" className="rounded-md border border-red-500/40 bg-red-500/10 p-3 text-sm">
              This condition changed after the freeze: its hash is no longer the frozen hash. The server refuses every research run on it (409 hash_mismatch) until it is frozen again.
            </p>
          )}
          <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 rounded-lg border p-3 text-sm">
            {(
              [
                ['Labels', `legal ${c.labels.legal}, harm ${c.labels.harm}`],
                ['Group', `${c.group}${c.sub_condition ? ` (${c.sub_condition})` : ''}, ${c.tag}`],
                ['Market preset', c.market_preset],
                ['Channels', `public ${onOff(c.channels.public)}, private pairs ${onOff(c.channels.private_pairs)}, news ${onOff(c.channels.news)}`],
                ['Call mode', c.call_mode],
                ['Talk kept from pricing', c.c3_isolation ? 'yes: the pricing call never sees the channel' : 'no'],
                ['Transparency', c.transparency],
                ['Observation', c.observation_mode],
                ['Models', sameModel(c.same_model_for_all_sellers)],
                ['Memory note', c.memory_summary],
                ['Hash', <Mono key="h">{c.hash}</Mono>],
                ['Frozen hash', c.frozen_hash ? <Mono key="f">{c.frozen_hash}</Mono> : 'none (draft)'],
                ['Version', String(c.version)],
              ] as [string, ReactNode][]
            ).map(([k, v]) => (
              <div key={k} className="contents">
                <dt className="text-muted-foreground">{k}</dt>
                <dd>{v}</dd>
              </div>
            ))}
          </dl>
          <section className="space-y-2">
            <h2 className="font-medium">Tools per seller</h2>
            <div className="grid gap-3 sm:grid-cols-2">
              {Object.entries(c.capabilities).map(([id, cap]) => (
                <div key={id} className="rounded-lg border p-3 text-sm">
                  <p className="font-medium">Seller {id}</p>
                  <ul className="mt-1 space-y-0.5 text-xs">
                    {cap.grants.map((g) => (
                      <li key={g.tool_id}>
                        <Mono>{g.tool_id}</Mono>
                        {Object.keys(g.params ?? {}).length > 0 && <span className="text-muted-foreground"> {JSON.stringify(g.params)}</span>}
                      </li>
                    ))}
                    <li className="text-muted-foreground">{readCallsText(c.limits, cap)}</li>
                  </ul>
                </div>
              ))}
            </div>
          </section>
          <section className="space-y-2">
            <h2 className="font-medium">Prompt variants</h2>
            {c.prompt_variants.map((v, i) => (
              <div key={v.id} className="space-y-2 rounded-lg border p-3">
                <p className="text-sm font-medium">
                  {v.id} <span className="font-normal text-muted-foreground">({variantSeedRule(c, i)})</span>
                </p>
                {Object.entries(v.sellers).map(([id, p]) => (
                  <div key={id} className="space-y-1 text-sm">
                    <p className="text-xs font-semibold text-muted-foreground">Seller {id}</p>
                    <p>
                      <span className="text-muted-foreground">System: </span>
                      {p.system_prompt}
                    </p>
                    <p>
                      <span className="text-muted-foreground">Talk: </span>
                      {p.talk_prompt ?? 'none: the talk call uses the system prompt'}
                    </p>
                  </div>
                ))}
              </div>
            ))}
          </section>
        </>
      )}
    </div>
  )
}
