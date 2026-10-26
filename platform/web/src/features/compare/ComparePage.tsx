import { useMemo } from 'react'
import { Link, useSearchParams } from 'react-router'
import { runPath } from '@/lib/paths'
import { X } from 'lucide-react'
import { useCompare } from '@/api/queries'
import { outputSpec, useIndicatorSpecs } from '@/api/indicators'
import { useRunIndicators } from '@/run/useRunIndicators'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { PriceChart, type ChartIndicator, type Overlay } from '@/components/chart/PriceChart'
import { useRunStreams } from '@/run/useRunStream'
import { useIndicatorSelection } from '@/run/useIndicatorSelection'
import { IndicatorMenu } from '@/features/run-view/IndicatorMenu'
import { KindBadge, StatusBadge } from '@/features/runs/badges'
import { usd } from '@/lib/format'

/** Short names for the chart: the first run is the base, the others are "run 2", "run 3", … */
const tag = (i: number) => `run ${i + 1}`
const show = (v: unknown) => (v === undefined ? '–' : typeof v === 'string' ? v : JSON.stringify(v))
const money = (n: number | undefined) => (n === undefined ? '–' : usd(n))

export function ComparePage() {
  const [params, setParams] = useSearchParams()
  const ids = useMemo(() => [...new Set((params.get('ids') ?? '').split(',').filter(Boolean))], [params])
  const cmp = useCompare(ids)
  const streams = useRunStreams(ids)
  const { selection, upsert, remove } = useIndicatorSelection()
  const specs = useIndicatorSpecs()
  const series = useRunIndicators(
    ids.map((id, i) => ({ id, stream: streams[i]!, status: cmp.data?.runs.find((r) => r.id === id)?.status })),
    selection,
  )
  const seriesError = series.error

  const drop = (id: string) =>
    setParams((p) => {
      p.set('ids', ids.filter((x) => x !== id).join(','))
      return p
    })

  const base = streams[0]?.state
  const overlays: Overlay[] = useMemo(() => streams.slice(1).map((s, i) => ({ id: ids[i + 1]!, label: tag(i + 1), state: s.state })), [streams, ids])

  // One chart indicator per selected indicator; each run adds its outputs, named with the run tag.
  const indicators: ChartIndicator[] = (specs.data ?? []).flatMap((spec) => {
    const outputs = series.data.flatMap((data) => {
      if (data.indicator_id !== spec.id) return []
      const i = ids.indexOf(data.run_id)
      return Object.entries(data.outputs).flatMap(([name, values]) => {
        const kind = outputSpec(spec, name)?.kind ?? 'line'
        if (kind !== 'line' && kind !== 'histogram') return []
        // Bars of several runs in one pane hide each other, so every output is a line here.
        return [{ name: `${name} · ${tag(i)}`, kind: 'line' as const, points: values.flatMap((v, j) => (v === null ? [] : [{ t: data.t[j]!, value: v }])) }]
      })
    })
    return outputs.length ? [{ key: spec.id, label: spec.name, pane: spec.pane, outputs }] : []
  })

  if (ids.length < 2) {
    return (
      <div className="py-16 text-center">
        <p className="text-lg font-medium">Select 2 or more runs to compare.</p>
        <Link to="/runs" className="text-sm underline underline-offset-4">
          Go to the run list
        </Link>
      </div>
    )
  }

  const runs = cmp.data?.runs ?? []
  const diff = cmp.data?.config_diff ?? []

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">Compare {ids.length} runs</h1>
        <p className="text-sm text-muted-foreground">Prices on one time axis, then every config field that differs. Run 1 is solid; the others are dotted.</p>
      </div>

      {cmp.isError && (
        <p role="alert" className="rounded-md border border-red-500/40 bg-red-500/10 p-3 text-sm">
          {cmp.error.message}
        </p>
      )}

      <ol className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3" aria-label="Runs">
        {ids.map((id, i) => {
          const r = runs.find((x) => x.id === id)
          return (
            <li key={id} className="flex items-start gap-2 rounded-lg border p-2 text-sm">
              <span className="mt-0.5 shrink-0 rounded bg-muted px-1.5 text-xs font-medium">{tag(i)}</span>
              <div className="min-w-0 flex-1 space-y-1">
                <Link className="block truncate font-mono underline-offset-2 hover:underline" to={runPath(id)}>
                  {id}
                </Link>
                {r && (
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
                    <KindBadge kind={r.kind} />
                    <StatusBadge status={r.status} />
                    <span>{r.condition_id ?? 'no group'}</span>
                    <span>seed {r.seed}</span>
                    <span>{money(r.cost_usd)}</span>
                    {r.models?.length ? <span>{r.models.join(', ')}</span> : null}
                  </div>
                )}
              </div>
              <Button size="icon" variant="ghost" className="size-7" aria-label={`Remove ${id} from the comparison`} disabled={ids.length <= 2} onClick={() => drop(id)}>
                <X />
              </Button>
            </li>
          )
        })}
      </ol>

      <div className="rounded-lg border p-2">
        <div className="px-1 pb-2">
          <IndicatorMenu selection={selection} upsert={upsert} remove={remove} error={seriesError} />
        </div>
        {base ? <PriceChart state={base} overlays={overlays} indicators={indicators} /> : <Skeleton className="h-80 w-full" />}
        <p className="px-1 pt-2 text-xs text-muted-foreground">
          Solid: {tag(0)} per seller, with its Nash (grey) and joint optimum (red). Dotted: {ids.slice(1).map((_, i) => tag(i + 1)).join(', ')}, in the same seller colours.
          {cmp.data && ` Shared axis R${cmp.data.axis.t_min ?? '?'} to R${cmp.data.axis.t_max ?? '?'}.`}
        </p>
      </div>

      <section className="space-y-2">
        <h2 className="font-medium">Config differences</h2>
        {cmp.isPending && <Skeleton className="h-24 w-full" />}
        {cmp.isSuccess && diff.length === 0 && <p className="text-sm text-muted-foreground">The configs are equal.</p>}
        {diff.length > 0 && (
          <div className="overflow-x-auto rounded-lg border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Field</TableHead>
                  {ids.map((id, i) => (
                    <TableHead key={id} title={id}>
                      {tag(i)}
                    </TableHead>
                  ))}
                </TableRow>
              </TableHeader>
              <TableBody className="font-mono text-xs">
                {diff.map((row) => (
                  <TableRow key={row.path} className="align-top">
                    <TableCell className="whitespace-nowrap">{row.path}</TableCell>
                    {ids.map((id) => (
                      <TableCell key={id} className="max-w-64 break-all whitespace-normal">
                        {show((row.values as Record<string, unknown>)[id])}
                      </TableCell>
                    ))}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </section>
    </div>
  )
}
