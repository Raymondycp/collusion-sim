import { useMemo, useState, type ReactNode } from 'react'
import { Calculator, Play } from 'lucide-react'
import { ApiError } from '@/api/client'
import { useIndicatorSpecs } from '@/api/indicators'
import { useProfiles } from '@/api/providers'
import {
  estimateRun,
  startRuns,
  useConditions,
  useDefaults,
  usePolicies,
  useRoles,
  useRunConfigSchema,
  useScenarios,
  useSeeds,
  useTools,
  changedSinceFreeze,
  devRefusal,
  type CostEstimate,
  type Preset,
  type RunConfig,
} from '@/api/runConfig'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { getAt, setAt, type Path } from '@/lib/jsonSchema'
import { AgentsEditor } from './AgentsEditor'
import { applyCondition, clearLocked, conditionValue, sameModelNote, starterDraft, type Registries } from './draft'
import { capRows, CapUse, capsText, EstimateNotes, NO_PRICE_NOTE, overText, raisesFor, type Cap, type Raise } from '@/components/budget'
import { usd } from '@/lib/format'
import { FieldError, CopyHash, FieldShell, fieldId, hasCap, LockedValue, NO_CAP_RE, NO_CAP_TEXT, NativeSelect, SchemaField, type FormCtx } from './SchemaField'

export type Kind = RunConfig['kind']

/** Fields the forms never show. */
export const NOT_SHOWN = new Set(['kind', 'scenario_hash', 'preset_id'])
const RESEARCH_ONLY = new Set(['condition_id', 'prompt_variant_id', 'split'])

function LoadError({ e }: { e: Error }) {
  return (
    <p role="alert" className="text-xs text-red-600 dark:text-red-400">
      {e.message}
    </p>
  )
}

/**
 * The caps that are below the worst case of one run, each with the value that clears it. The worst case counts every allowed
 * tool step of every call, so a real run makes far fewer calls than a cap that clears it (FE-realbackend finding 2). The
 * page offers the raise in one click and estimates again, and the server still decides the gate. The run estimate checks the caps of
 * each run only; the batch budget is checked by the experiment estimate (CR-87 minor 2).
 */
export function capsBelowWorstCase(e: CostEstimate, budget: RunConfig['budget'] | undefined): Raise[] {
  const t = e.per_run.tokens_max
  return raisesFor(budget, { calls: e.per_run.calls?.total_max, tokens: t ? (t.input ?? 0) + (t.output ?? 0) : null, usd: e.per_run.usd_max }, 'max', 'each run')
}

function EstimateView({ e, budget, onRaise }: { e: CostEstimate; budget?: RunConfig['budget']; onRaise?: (cap: Cap, value: number) => void }) {
  const c = e.per_run.calls
  const raises = onRaise ? capsBelowWorstCase(e, budget) : []
  return (
    <div className="space-y-1 text-sm">
      <p>
        {e.n_runs} run{e.n_runs === 1 ? '' : 's'}, at most {c?.total_max ?? '?'} calls each ({e.call_mode}). Maximum cost:{' '}
        <strong>{usd(e.total_usd_max)}</strong>.
      </p>
      <CapUse scope="of one run" rows={capRows(budget, { calls: e.per_run.calls?.total_max, tokens: e.per_run.tokens_max ? (e.per_run.tokens_max.input ?? 0) + (e.per_run.tokens_max.output ?? 0) : null, usd: e.per_run.usd_max })} />
      <EstimateNotes calls={e.calls_max} tokensMax={e.tokens_max} rateWindow={e.rate_window} warnings={e.warnings} />
      {e.pricing_missing_for?.length ? (
        <p className="text-amber-700 dark:text-amber-400">
          {e.pricing_missing_for.join(', ')} {NO_PRICE_NOTE}
        </p>
      ) : null}
      {e.within_budget === false && (
        <div className="space-y-1.5 text-red-600 dark:text-red-400">
          <p>
            {raises.length > 0
              ? `The worst case is above the ${overText(raises)}. Raise a cap, or reduce rounds or agents.`
              : 'The worst case of a run is above the budget. Raise a cap, or reduce rounds or agents.'}
          </p>
          {raises.length > 0 && (
            <p className="text-xs text-muted-foreground">
              The worst case counts every allowed tool step and every retry of every call, so a run makes far fewer calls. A cap below the worst case can still stop a run early (end reason budget_exceeded). A cap at the worst case never does.
              After a raise, a cap allows {raises.map((r) => r.spend).join(', ')}.
            </p>
          )}
          {raises.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {raises.map((r) => (
                <Button key={r.cap} type="button" variant="outline" size="sm" onClick={() => onRaise?.(r.cap, r.value)}>
                  Raise {r.label} and estimate again
                </Button>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/**
 * The run form's state and actions, for the New run page and the workbench setup mode. Start goes through the same server
 * gates in both (UI-WORKBENCH.md §3): the page shows the refusal, it never decides a gate itself.
 * `onStarted` gets the new run ids. Returns `{ loadError }` while a registry fails, and null while they load.
 */
export function useRunDraft(onStarted: (runIds: string[], kind: Kind) => void) {
  const schema = useRunConfigSchema()
  const roles = useRoles()
  const tools = useTools()
  const policies = usePolicies()
  const profiles = useProfiles()
  const scenarios = useScenarios()
  const conditions = useConditions()
  const seeds = useSeeds()
  const indicators = useIndicatorSpecs()
  // Optional: without it (an older server), new agents start with the first profile that has a key.
  const defaults = useDefaults()

  const queries = [schema, roles, tools, policies, profiles, scenarios]
  const loadError = queries.find((q) => q.error)?.error
  const reg: Registries | null = useMemo(
    () =>
      schema.data && roles.data && tools.data && policies.data && profiles.data && scenarios.data && !defaults.isPending
        ? { root: schema.data, roles: roles.data, tools: tools.data, policies: policies.data, profiles: profiles.data, scenarios: scenarios.data, defaults: defaults.data }
        : null,
    [schema.data, roles.data, tools.data, policies.data, profiles.data, scenarios.data, defaults.isPending, defaults.data],
  )

  const [kind, setKind] = useState<Kind>('exploratory')
  const [drafts, setDrafts] = useState<Partial<Record<Kind, RunConfig>>>({})
  const [rev, setRev] = useState(0)
  const [errors, setErrors] = useState<{ status?: number; code?: string; message: string; fields: Record<string, string> } | null>(null)
  const [estimate, setEstimate] = useState<{ for: string; e: CostEstimate } | null>(null)
  const [busy, setBusy] = useState(false)
  const [confirm, setConfirm] = useState(false)

  if (loadError) return { loadError }
  if (!reg) return null

  const draft = drafts[kind] ?? starterDraft(kind, reg)
  const research = kind === 'research'
  const condition = conditions.data?.find((c) => c.id === draft.condition_id)
  const replace = (d: RunConfig) => {
    setDrafts((all) => ({ ...all, [d.kind]: d }))
    setRev((r) => r + 1)
  }
  const set = (path: Path, value: unknown) => setDrafts((all) => ({ ...all, [kind]: setAt(all[kind] ?? draft, path, value) }))
  const ctx: FormCtx = {
    root: reg.root,
    research,
    errors: errors?.fields ?? {},
    set,
    lockedValue: (p) => conditionValue(draft, condition, p),
  }
  // A research request leaves every locked field to the condition and the seed map, whatever the draft holds.
  const request = research ? clearLocked(draft, condition, reg.root) : draft
  const json = JSON.stringify(request)
  const fresh = estimate?.for === json ? estimate.e : null

  const fail = (e: unknown) => {
    // The server's validation text names Python fields ("Value error, set at least one of max_usd, ..."): say it in plain words.
    const plain = (m: string) => (NO_CAP_RE.test(m) ? NO_CAP_TEXT : m)
    // The plain sentence stands alone: no "422 validation_error" prefix and no second line for the budget field (cr E2E obs 4).
    if (e instanceof ApiError && NO_CAP_RE.test(e.message)) setErrors({ message: NO_CAP_TEXT, fields: {} })
    else if (e instanceof ApiError) setErrors({ status: e.status, code: e.code, message: plain(e.message), fields: Object.fromEntries(e.fields.map((f) => [f.path, plain(f.message)])) })
    else setErrors({ message: 'The request failed.', fields: {} })
  }

  async function runEstimate(of: RunConfig = draft) {
    const req = research ? clearLocked(of, condition, reg!.root) : of
    setBusy(true)
    setErrors(null)
    try {
      setEstimate({ for: JSON.stringify(req), e: await estimateRun(req) })
    } catch (e) {
      setEstimate(null)
      fail(e)
    } finally {
      setBusy(false)
    }
  }

  async function start() {
    setBusy(true)
    try {
      const r = await startRuns(request)
      setConfirm(false)
      // The setup page stays mounted (workbench), so the next Start needs a new estimate, not one click to a repeat run.
      setEstimate(null)
      onStarted(r.run_ids, draft.kind)
    } catch (e) {
      setConfirm(false)
      fail(e)
    } finally {
      setBusy(false)
    }
  }

  const props = reg.root.properties ?? {}
  const required = reg.root.required ?? []

  const field = (name: string): ReactNode => {
    if (!props[name] || (!research && RESEARCH_ONLY.has(name))) return null
    const path = [name]
    switch (name) {
      case 'scenario_id': {
        if (research) return <LockedValue key={name} label="Scenario" value={`One scenario per seed, from the seed map (seeds hash ${seeds.data?.hash ?? '…'})`} />
        const sc = reg.scenarios.find((s) => s.id === draft.scenario_id)
        return (
          <FieldShell key={name} path={path} label="Scenario" help={sc?.hash ? <CopyHash hash={sc.hash} /> : undefined}>
            <NativeSelect
              id={fieldId(path)}
              value={draft.scenario_id ?? ''}
              onChange={(e) => {
                const s = reg.scenarios.find((x) => x.id === e.target.value)
                set([], { ...draft, scenario_id: e.target.value, scenario_hash: s?.hash ?? '' })
              }}
            >
              {reg.scenarios.map((s) => (
                <option key={s.id} value={s.id} disabled={s.status !== 'frozen'}>
                  {s.name}
                  {s.status !== 'frozen' ? ' (draft, not frozen)' : ''}
                </option>
              ))}
            </NativeSelect>
          </FieldShell>
        )
      }
      case 'condition_id':
        return (
          <FieldShell key={name} path={path} label="Condition" help={condition ? `Labels from the condition: legal ${condition.labels.legal}, harm ${condition.labels.harm}. They cannot be changed here.` : 'The condition sets the labels, channels, prompts and tools.'}>
            <NativeSelect
              id={fieldId(path)}
              value={draft.condition_id ?? ''}
              onChange={(e) => {
                const c = conditions.data?.find((x) => x.id === e.target.value)
                if (c) replace(applyCondition(draft, c, reg))
              }}
            >
              <option value="">Select…</option>
              {conditions.data?.map((c) => (
                <option key={c.id} value={c.id} disabled={changedSinceFreeze(c)}>
                  {c.id}: {c.name}
                  {changedSinceFreeze(c) ? ' (changed since the freeze; the server refuses it)' : ''}
                </option>
              ))}
            </NativeSelect>
            {conditions.error && <LoadError e={conditions.error} />}
          </FieldShell>
        )
      case 'prompt_variant_id':
        return condition && <LockedValue key={name} label="Prompt variant" value={conditionValue(draft, condition, path)} />
      case 'split':
        return (
          <FieldShell key={name} path={path} label="Split">
            <NativeSelect id={fieldId(path)} value={draft.split ?? ''} onChange={(e) => set([], { ...draft, split: e.target.value as RunConfig['split'], seeds: [] })}>
              <option value="pilot">pilot</option>
              <option value="dev" disabled={!!condition && !!devRefusal(condition)}>
                dev{condition && devRefusal(condition) ? ` (${devRefusal(condition)})` : ''}
              </option>
              <option value="test" disabled={seeds.data?.test_locked !== false}>
                test{seeds.data?.test_locked !== false ? ' (locked until the freeze)' : ''}
              </option>
            </NativeSelect>
          </FieldShell>
        )
      case 'seeds': {
        if (!research) return <SchemaField key={`${name}-${rev}`} path={path} name={name} schema={props[name]!} value={draft.seeds} required />
        const pool = (draft.split === 'pilot' || draft.split === 'dev' || draft.split === 'test' ? seeds.data?.[draft.split] : undefined) ?? []
        const chosen = new Set(draft.seeds)
        return (
          <fieldset key={name} className="space-y-2">
            <legend className="text-sm font-medium">Seeds ({draft.seeds.length} selected, one run each)</legend>
            <div className="flex flex-wrap gap-1.5">
              {pool.map((s) => (
                <label key={s} className="flex items-center gap-1 rounded-md border px-2 py-1 font-mono text-xs">
                  <input type="checkbox" className="size-3.5" checked={chosen.has(s)} onChange={(e) => set(path, e.target.checked ? [...draft.seeds, s].sort((a, b) => a - b) : draft.seeds.filter((x) => x !== s))} />
                  {s}
                </label>
              ))}
            </div>
            {seeds.error && <LoadError e={seeds.error} />}
            <Button type="button" variant="ghost" size="sm" onClick={() => set(path, pool)}>
              Select all {pool.length}
            </Button>
            <FieldError path={path} />
          </fieldset>
        )
      }
      case 'agents':
        return <AgentsEditor key={name} agents={draft.agents} request={request} reg={reg} condition={condition} />
      case 'indicators': {
        const on = new Set((draft.indicators ?? []).map((i) => i.id))
        return (
          <fieldset key={name} className="space-y-1">
            <legend className="text-sm font-medium">Indicators shown when the run view opens</legend>
            {indicators.data?.map((s) => (
              <label key={s.id} className="flex items-center gap-2 text-sm">
                <input type="checkbox" className="size-4" checked={on.has(s.id)} onChange={(e) => set(path, e.target.checked ? [...(draft.indicators ?? []), { id: s.id }] : (draft.indicators ?? []).filter((i) => i.id !== s.id))} />
                {s.name}
              </label>
            ))}
          </fieldset>
        )
      }
      default:
        return <SchemaField key={`${name}-${rev}`} path={path} name={name} schema={props[name]!} value={getAt(draft, path)} required={required.includes(name)} />
    }
  }

  return {
    reg,
    kind,
    /** Switches between the exploratory and the research draft. Each kind keeps its own draft. */
    chooseKind: (k: Kind) => {
      setKind(k)
      setErrors(null)
      setRev((r) => r + 1)
    },
    loadPreset: (p: Preset) => {
      setKind(p.config.kind)
      setErrors(null)
      replace({ ...structuredClone(p.config), preset_id: p.id })
    },
    draft,
    research,
    condition,
    request,
    ctx,
    rev,
    field,
    /** The top-level field names in schema order, without the ones the forms never show. */
    fieldNames: Object.keys(props).filter((k) => !NOT_SHOWN.has(k)),
    note: sameModelNote(draft, condition),
    seedMap: seeds.data,
    start: {
      errors,
      fresh,
      stale: !!estimate && !fresh,
      busy,
      confirm,
      setConfirm,
      runEstimate: () => runEstimate(),
      /** Sets one budget cap and estimates again, in one click (the Start rule, FE-realbackend finding 2). */
      raise: (cap: Cap, value: number) => {
        const next = setAt(draft, ['budget', cap], value) as RunConfig
        replace(next)
        return runEstimate(next)
      },
      start,
    },
  }
}

export type RunDraft = Extract<ReturnType<typeof useRunDraft>, { reg: Registries }>

/** Errors, the estimate and the Estimate and Start buttons. Start needs a fresh estimate within budget. */
export function StartBar({ d }: { d: RunDraft }) {
  const { errors, fresh, stale, busy, setConfirm, runEstimate, raise } = d.start
  return (
    <div className="space-y-2">
      {errors && (
        <p role="alert" className="text-sm text-red-600 dark:text-red-400">
          {errors.code && (
            <code className="mr-1 font-mono text-xs">
              {errors.status ? `${errors.status} ` : ''}
              {errors.code}:
            </code>
          )}
          {errors.message}
          {Object.keys(errors.fields).length > 1 ? ` (${Object.keys(errors.fields).length} fields; see the marked fields above)` : ''}
        </p>
      )}
      {fresh && <EstimateView e={fresh} budget={d.draft.budget} onRaise={raise} />}
      {stale && <p className="text-xs text-muted-foreground">The form changed after the estimate. Estimate again before you start.</p>}
      <div className="flex flex-wrap gap-2">
        <Button type="button" variant="outline" disabled={busy || !hasCap(d.draft.budget)} title={hasCap(d.draft.budget) ? undefined : NO_CAP_TEXT} onClick={runEstimate}>
          <Calculator /> Estimate cost
        </Button>
        <Button type="button" disabled={busy || !fresh || fresh.within_budget === false} onClick={() => setConfirm(true)} title={fresh ? undefined : 'Estimate the cost first.'}>
          <Play /> Start {d.draft.seeds.length} run{d.draft.seeds.length === 1 ? '' : 's'}
        </Button>
        {fresh?.within_budget === false && (
          <Button type="button" variant="outline" disabled={busy} onClick={() => setConfirm(true)} title="The server allows this start. A cap below the worst case can stop a run early.">
            Start anyway
          </Button>
        )}
      </div>
    </div>
  )
}

/** The confirmation after Estimate: what the runs may call and cost. */
export function StartDialog({ d }: { d: RunDraft }) {
  const { fresh, confirm, setConfirm, busy, start } = d.start
  const { request, draft } = d
  if (!confirm || !fresh) return null
  return (
    <Dialog open onOpenChange={(o) => !o && setConfirm(false)}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            Start {fresh.n_runs} {draft.kind} run{fresh.n_runs === 1 ? '' : 's'}?
          </DialogTitle>
          <DialogDescription>
            {request.agents.some((a) => a.policy.kind === 'llm') ? (
              <>
                LLM agents call their providers. Each run stops at its budget ({capsText(request.budget)})
                {request.batch_budget ? `, and the batch stops at ${capsText(request.batch_budget)}` : ''}.
                {fresh.within_budget === false ? ' The worst case is above a cap, so a run can stop early (end reason budget_exceeded).' : ''}
              </>
            ) : (
              'Every agent is scripted, so the runs make no model calls.'
            )}
          </DialogDescription>
        </DialogHeader>
        <EstimateView e={fresh} />
        <DialogFooter>
          <Button variant="ghost" onClick={() => setConfirm(false)}>
            Cancel
          </Button>
          <Button disabled={busy} onClick={start}>
            Start
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
