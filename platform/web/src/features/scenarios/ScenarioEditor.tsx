import { useState, type ReactNode } from 'react'
import { Plus, Trash2, Wand2 } from 'lucide-react'
import { asApiError, type ApiError } from '@/api/client'
import { solveDemand, type DemandModel, type ScenarioDraft, type ScenarioEvent, type Shock } from '@/api/scenarios'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { defaultFor, setAt } from '@/lib/jsonSchema'
import { NativeSelect, TextArea } from '@/features/new-run/SchemaField'
import { newEvent, QUIET_WINDOW, resizeSellers, sellerIds, shockPaths } from './scenario'
import { SchemaForm } from './SchemaForm'

type Errors = Record<string, string>
type Props = { draft: ScenarioDraft; onChange: (d: ScenarioDraft) => void; models: DemandModel[]; errors: Errors }

const num = (v: string) => (v === '' ? null : Number(v))
const under = (errors: Errors, prefix: string) =>
  Object.fromEntries(Object.entries(errors).flatMap(([k, v]) => (k === prefix ? [['', v]] : k.startsWith(`${prefix}.`) ? [[k.slice(prefix.length + 1), v]] : [])))

export function Section({ title, children, action }: { title: string; children: ReactNode; action?: ReactNode }) {
  return (
    <section className="space-y-3 rounded-xl border p-4">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="font-medium">{title}</h2>
        <div className="ml-auto flex gap-2">{action}</div>
      </div>
      {children}
    </section>
  )
}

function Field({ id, label, error, help, children }: { id: string; label: string; error?: string; help?: string; children: ReactNode }) {
  return (
    <div className="space-y-1">
      <label htmlFor={id} className="text-sm font-medium">
        {label}
      </label>
      {children}
      {help && <p className="text-xs text-muted-foreground">{help}</p>}
      {error && (
        <p role="alert" className="text-xs text-red-600 dark:text-red-400">
          {error}
        </p>
      )}
    </div>
  )
}

/** While the field has focus it shows what the person types, so clearing "30" to type "40" never passes through a fallback value. Empty sends null. */
export function NumberInput({ id, value, onChange, step = 'any', min, max }: { id: string; value: number | null | undefined; onChange: (v: number | null) => void; step?: string | number; min?: number; max?: number }) {
  const [text, setText] = useState<string | null>(null)
  return (
    <Input
      id={id}
      type="number"
      step={step}
      min={min}
      max={max}
      value={text ?? value ?? ''}
      onFocus={() => setText(value == null ? '' : String(value))}
      onBlur={() => setText(null)}
      onChange={(e) => {
        setText(e.target.value)
        onChange(num(e.target.value))
      }}
    />
  )
}

function DemandHelper({ model, onParams }: { model: DemandModel; onParams: (p: Record<string, unknown>) => void }) {
  const schema = model.solver_schema
  const [targets, setTargets] = useState<Record<string, unknown>>(() => (schema ? (defaultFor(schema, schema) as Record<string, unknown>) : {}))
  const [result, setResult] = useState<Awaited<ReturnType<typeof solveDemand>> | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  if (!schema) return null
  const solve = async () => {
    setError(null)
    setResult(null)
    try {
      setResult(await solveDemand(model.id, targets))
    } catch (e) {
      setError(asApiError(e))
    }
  }
  return (
    <details className="rounded-lg border p-3">
      <summary className="cursor-pointer text-sm font-medium">Demand helper: target prices in, params out</summary>
      <div className="mt-3 space-y-3">
        {schema.description && <p className="text-xs text-muted-foreground">{schema.description}</p>}
        <SchemaForm id="solver" legend="Targets" schema={schema} value={targets} onChange={setTargets} />
        <Button variant="outline" size="sm" onClick={() => void solve()}>
          <Wand2 /> Solve
        </Button>
        {error && (
          <p role="alert" className="text-sm text-red-600 dark:text-red-400">
            {error.message} {error.fields.map((f) => f.message).join(' ')}
          </p>
        )}
        {result && (
          <div className="space-y-2 rounded-md bg-muted/50 p-2 text-sm">
            <p>
              Params {Object.entries(result.params).filter(([, v]) => typeof v === 'number').map(([k, v]) => `${k} = ${v}`).join(', ')}. Nash {result.nash}, joint optimum {result.joint_optimum}.
            </p>
            {result.problems.map((p) => (
              <p key={p} className="text-amber-700 dark:text-amber-400">
                {p}
              </p>
            ))}
            <Button size="sm" onClick={() => onParams(result.params)}>
              Use these params
            </Button>
          </div>
        )}
      </div>
    </details>
  )
}

function ShockRow({ id, shock, paths, onChange, onRemove }: { id: string; shock: Shock; paths: string[]; onChange: (s: Shock) => void; onRemove: () => void }) {
  return (
    <div className="flex flex-wrap items-end gap-2">
      <Field id={`${id}-path`} label="Param">
        <NativeSelect id={`${id}-path`} value={shock.path} onChange={(e) => onChange({ ...shock, path: e.target.value })} className="w-28">
          {!paths.includes(shock.path) && <option value={shock.path}>{shock.path || 'Select…'}</option>}
          {paths.map((p) => (
            <option key={p} value={p}>
              {p.slice(1)}
            </option>
          ))}
        </NativeSelect>
      </Field>
      <Field id={`${id}-op`} label="Change">
        <NativeSelect id={`${id}-op`} value={shock.op} onChange={(e) => onChange({ ...shock, op: e.target.value as Shock['op'] })} className="w-24">
          <option value="add">add</option>
          <option value="mul">multiply</option>
          <option value="set">set to</option>
        </NativeSelect>
      </Field>
      <Field id={`${id}-value`} label="Value">
        <div className="w-24">
          <NumberInput id={`${id}-value`} value={shock.value} onChange={(v) => onChange({ ...shock, value: v as number })} />
        </div>
      </Field>
      <Button variant="ghost" size="icon" aria-label="Remove shock" onClick={onRemove}>
        <Trash2 />
      </Button>
    </div>
  )
}

function EventEditor({ i, e, sellers, paths, errors, onChange, onRemove }: { i: number; e: ScenarioEvent; sellers: string[]; paths: string[]; errors: Errors; onChange: (e: ScenarioEvent) => void; onRemove: () => void }) {
  const id = `ev${i}`
  const shocks = e.shocks ?? []
  const set = <K extends keyof ScenarioEvent>(k: K, v: ScenarioEvent[K]) => onChange({ ...e, [k]: v })
  const newsOnly = e.kind === 'news_only'
  return (
    <fieldset className="space-y-3 rounded-lg border p-3">
      <legend className="px-1 text-sm font-medium">Event {i + 1}</legend>
      {errors[''] && (
        <p role="alert" className="text-xs text-red-600 dark:text-red-400">
          {errors['']}
        </p>
      )}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Field id={`${id}-kind`} label="Kind">
          <NativeSelect
            id={`${id}-kind`}
            value={e.kind ?? 'param_shock'}
            onChange={(x) =>
              onChange(
                x.target.value === 'news_only'
                  ? { ...e, kind: 'news_only', effect: null, length: null, shocks: [] }
                  : { ...newEvent(sellers, e.announce, paths), visible: e.visible, news: e.news, news_source: e.news_source, misleading: e.misleading },
              )
            }
          >
            <option value="param_shock">Param shock</option>
            <option value="news_only">News only (placebo)</option>
          </NativeSelect>
        </Field>
        <Field id={`${id}-announce`} label="News in round" error={errors.announce}>
          <NumberInput id={`${id}-announce`} step={1} min={1} value={e.announce} onChange={(v) => set('announce', v ?? 1)} />
        </Field>
        {!newsOnly && (
          <>
            <Field id={`${id}-effect`} label="Shock from round" error={errors.effect}>
              <NumberInput id={`${id}-effect`} step={1} min={1} value={e.effect} onChange={(v) => set('effect', v)} />
            </Field>
            <Field id={`${id}-length`} label="Rounds" error={errors.length}>
              <NumberInput id={`${id}-length`} step={1} min={1} value={e.length} onChange={(v) => set('length', v)} />
            </Field>
          </>
        )}
      </div>
      {!newsOnly && (
        <div className="space-y-2">
          <p className="text-sm font-medium">Shocks</p>
          {shocks.length === 0 && <p className="text-xs text-amber-700 dark:text-amber-400">A parameter shock needs at least one shock. Add one, or make the event news only.</p>}
          {shocks.map((s, j) => (
            <ShockRow
              key={j}
              id={`${id}-s${j}`}
              shock={s}
              paths={paths}
              onChange={(x) => set('shocks', shocks.map((y, k) => (k === j ? x : y)))}
              onRemove={() => set('shocks', shocks.filter((_, k) => k !== j))}
            />
          ))}
          <Button variant="outline" size="sm" onClick={() => set('shocks', [...shocks, { path: paths[0] ?? '', op: 'add', value: 0 }])}>
            <Plus /> Add shock
          </Button>
        </div>
      )}
      <fieldset className="space-y-1">
        <legend className="text-sm font-medium">Sellers who see the news</legend>
        <div className="flex flex-wrap gap-3">
          {sellers.map((s) => (
            <label key={s} className="flex items-center gap-1.5 text-sm">
              <input type="checkbox" className="size-4" checked={e.visible.includes(s)} onChange={(x) => set('visible', x.target.checked ? sellers.filter((y) => y === s || e.visible.includes(y)) : e.visible.filter((y) => y !== s))} />
              {s}
            </label>
          ))}
        </div>
        {errors.visible && <p className="text-xs text-red-600 dark:text-red-400">{errors.visible}</p>}
      </fieldset>
      <Field id={`${id}-news`} label="News text" help={e.news_source === 'ai' ? 'Written by AI. Editing it marks it as written by a person.' : undefined} error={errors.news}>
        <TextArea id={`${id}-news`} className="font-sans text-sm" value={e.news ?? ''} onChange={(x) => onChange({ ...e, news: x.target.value, news_source: 'human' })} />
      </Field>
      <div className="flex flex-wrap items-center gap-4">
        <label className="flex items-center gap-1.5 text-sm">
          <input type="checkbox" className="size-4" checked={e.misleading ?? false} onChange={(x) => set('misleading', x.target.checked)} />
          Misleading on purpose (the check allows news that disagrees with the shock)
        </label>
        <Button variant="ghost" size="sm" className="ml-auto" onClick={onRemove}>
          <Trash2 /> Remove event
        </Button>
      </div>
    </fieldset>
  )
}

export function ScenarioEditor({ draft, onChange, models, errors }: Props) {
  const model = models.find((m) => m.id === draft.market.demand.id)
  const sellers = sellerIds(draft)
  const set = (path: (string | number)[], v: unknown) => onChange(setAt(draft, path, v))
  const quiet = draft.quiet_window ?? QUIET_WINDOW
  const paths = shockPaths(model)
  const params = draft.market.demand.params
  const onParams = (p: Record<string, unknown>) => set(['market', 'demand', 'params'], p)

  return (
    <div className="space-y-4">
      <Section title="Basics">
        <div className="grid gap-3 sm:grid-cols-2">
          <Field id="scn-name" label="Name" error={errors.name}>
            <Input id="scn-name" value={draft.name} maxLength={120} onChange={(e) => set(['name'], e.target.value)} />
          </Field>
          <Field id="scn-sellers" label="Sellers" help={`Seller ids: ${sellers.join(', ')}`} error={errors.n_sellers ?? errors.seller_ids}>
            <NumberInput
              id="scn-sellers"
              step={1}
              min={2}
              max={6}
              value={draft.n_sellers}
              onChange={(v) => {
                if (v === null || !Number.isInteger(v) || v < 2 || v > 6) return
                const ids = resizeSellers(sellers, v)
                onChange({ ...draft, n_sellers: v, seller_ids: ids, events: draft.events.map((e) => ({ ...e, visible: e.visible.filter((s) => ids.includes(s)) })) })
              }}
            />
          </Field>
        </div>
        <Field id="scn-description" label="Description" error={errors.description}>
          <TextArea id="scn-description" className="font-sans text-sm" value={draft.description ?? ''} onChange={(e) => set(['description'], e.target.value)} />
        </Field>
        <div className="grid grid-cols-3 gap-3">
          <Field id="scn-min" label="Min rounds" error={errors['horizon.min_rounds'] ?? errors.horizon}>
            <NumberInput id="scn-min" step={1} min={1} max={200} value={draft.horizon.min_rounds} onChange={(v) => set(['horizon', 'min_rounds'], v)} />
          </Field>
          <Field id="scn-max" label="Max rounds" error={errors['horizon.max_rounds']}>
            <NumberInput id="scn-max" step={1} min={1} max={200} value={draft.horizon.max_rounds} onChange={(v) => set(['horizon', 'max_rounds'], v)} />
          </Field>
          <Field id="scn-stop" label="Stop chance" help="Per round after the minimum." error={errors['horizon.stop_prob']}>
            <NumberInput id="scn-stop" min={0} max={1} value={draft.horizon.stop_prob} onChange={(v) => set(['horizon', 'stop_prob'], v)} />
          </Field>
        </div>
        <fieldset className="space-y-1">
          <legend className="text-sm font-medium">Quiet window</legend>
          <p className="text-xs text-muted-foreground">Every scenario has one: no announcement and no change in these rounds (the probe rounds). It must end before the minimum number of rounds.</p>
          <div className="grid max-w-xs grid-cols-2 gap-3">
            <Field id="scn-quiet-from" label="From round">
              <NumberInput id="scn-quiet-from" step={1} min={1} value={quiet[0]} onChange={(v) => set(['quiet_window'], [v, quiet[1]])} />
            </Field>
            <Field id="scn-quiet-to" label="To round">
              <NumberInput id="scn-quiet-to" step={1} min={1} value={quiet[1]} onChange={(v) => set(['quiet_window'], [quiet[0], v])} />
            </Field>
          </div>
          {errors.quiet_window && <p className="text-xs text-red-600 dark:text-red-400">{errors.quiet_window}</p>}
        </fieldset>
      </Section>

      <Section title="Market">
        <Field id="scn-model" label="Demand model" error={errors['market.demand.id']}>
          <NativeSelect
            id="scn-model"
            value={model?.id ?? ''}
            onChange={(e) => {
              const m = models.find((x) => x.id === e.target.value)
              if (m) set(['market', 'demand'], { id: m.id, version: m.version, params: defaultFor(m.params_schema, m.params_schema) })
            }}
          >
            {!model && <option value="">{draft.market.demand.id ? `${draft.market.demand.id} (not registered)` : 'Select…'}</option>}
            {models.map((m) => (
              <option key={m.id} value={m.id}>
                {m.name}
              </option>
            ))}
          </NativeSelect>
        </Field>
        {model && (
          <>
            <p className="font-mono text-xs text-muted-foreground">{model.formula}</p>
            {model.description && <p className="text-xs text-muted-foreground">{model.description}</p>}
            <SchemaForm id="params" legend="Params" schema={model.params_schema} value={params} onChange={onParams} errors={under(errors, 'market.demand.params')} />
            <DemandHelper key={model.id} model={model} onParams={onParams} />
          </>
        )}
        <div className="grid grid-cols-3 gap-3">
          {(['min', 'max', 'step'] as const).map((k) => (
            <Field key={k} id={`scn-grid-${k}`} label={`Price grid ${k}`} error={errors[`market.grid.${k}`]}>
              <NumberInput id={`scn-grid-${k}`} min={0} value={draft.market.grid?.[k]} onChange={(v) => set(['market', 'grid', k], v)} />
            </Field>
          ))}
        </div>
        <p className="text-xs text-muted-foreground">Market presets (DECISIONS-7 §5) are designed, not built: the contract has no GET /market-presets yet.</p>
      </Section>

      <Section
        title="Events"
        action={
          <Button variant="outline" size="sm" onClick={() => set(['events'], [...draft.events, newEvent(sellers, Math.max(1, ...draft.events.map((e) => (e.effect ?? e.announce) + (e.length ?? 0) + 1)), paths)])}>
            <Plus /> Add event
          </Button>
        }
      >
        {draft.events.length === 0 && <p className="text-sm text-muted-foreground">No events. The market stays the same for every round.</p>}
        {draft.events.map((e, i) => (
          <EventEditor
            key={i}
            i={i}
            e={e}
            sellers={sellers}
            paths={paths}
            errors={under(errors, `events.${i}`)}
            onChange={(x) => set(['events', i], x)}
            onRemove={() => set(['events'], draft.events.filter((_, k) => k !== i))}
          />
        ))}
        <p className="text-xs text-muted-foreground">AI news text (POST /scenarios/{'{id}'}/generate-news) is designed, not built: the route answers 501 and costs LLM calls.</p>
      </Section>
    </div>
  )
}
