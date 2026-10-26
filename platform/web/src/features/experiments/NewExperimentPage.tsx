import { useMemo, useState, type ReactNode } from 'react'
import { Link, useNavigate } from 'react-router'
import { Calculator, Play } from 'lucide-react'
import { asApiError } from '@/api/client'
import { DEFAULT_BRANCH_PLAN, estimateExperiment, startExperiment, type BranchPlan, type ExperimentCreate, type ExperimentEstimate } from '@/api/experiments'
import { useProfiles, type ProviderProfile } from '@/api/providers'
import { changedSinceFreeze, devRefusal, useConditions, usePolicies, useRoles, useRunConfigSchema, useScenarios, useSeeds, useTools } from '@/api/runConfig'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { setAt, type JSONSchema, type Path } from '@/lib/jsonSchema'
import { AgentsEditor } from '@/features/new-run/AgentsEditor'
import { sameModelNote, type Registries } from '@/features/new-run/draft'
import { FieldError, FieldShell, fieldId, Form, hasCap, NativeSelect, NO_CAP_RE, NO_CAP_TEXT, SchemaField, type FormCtx } from '@/features/new-run/SchemaField'
import { capRows, CapUse, EstimateNotes, NO_PRICE_NOTE, overText, raisesFor, type Cap, type RateWindowRow } from '@/components/budget'
import { usd } from '@/lib/format'
import { NumberInput } from '@/features/scenarios/ScenarioEditor'
import { describeChildren, experimentRequest, llmAgents, planGrid, runsOf, starterExperiment, withConditions, type LlmAgent } from './plan'

const count = (n: number, noun: string) => `${n} ${noun}${n === 1 ? '' : 's'}`

export const OPENING_PRICE_SCHEMA: JSONSchema = {
  type: ['number', 'null'],
  minimum: 0,
  default: null,
  title: 'Current market price at opening',
  description: 'Stated once in every seller’s market facts. A reference only: not a cap, and sellers price freely. Empty: not stated.',
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="space-y-3 rounded-xl border p-4">
      <h2 className="font-medium">{title}</h2>
      {children}
    </section>
  )
}

const money = usd

export function EstimateView({ e, batch, onRaise }: { e: ExperimentEstimate; batch?: ExperimentCreate['batch_budget']; onRaise?: (cap: Cap, value: number) => void }) {
  const t = e.tokens_max
  const raises = onRaise ? raisesFor(batch, { calls: e.calls_max, tokens: t ? (t.input ?? 0) + (t.output ?? 0) : null, usd: e.usd_max }, 'batch max', 'all runs') : []
  return (
    <div className="space-y-2 text-sm">
      <p>
        {e.n_cells} cell{e.n_cells === 1 ? '' : 's'}, {e.runs.total} runs ({e.runs.parents} parents, {e.runs.probe} probe, {e.runs.placebo} placebo, {e.runs.ablation} ablation). At most{' '}
        {e.calls_max.toLocaleString()} calls. Maximum cost: <strong>{money(e.usd_max)}</strong>.
      </p>
      <CapUse scope="of all runs" rows={capRows(batch, { calls: e.calls_max, tokens: t ? (t.input ?? 0) + (t.output ?? 0) : null, usd: e.usd_max })} />
      <EstimateNotes calls={e.calls_max} tokensMax={e.tokens_max} rateWindow={(e.rate_window ?? []) as RateWindowRow[]} warnings={e.warnings ?? []} />
      {e.pricing_missing_for.length > 0 && (
        <p className="text-amber-700 dark:text-amber-400">
          {e.pricing_missing_for.join(', ')} {NO_PRICE_NOTE}
        </p>
      )}
      {e.within_budget === false && (
        <div className="space-y-1.5 text-red-600 dark:text-red-400">
          <p>
            {raises.length > 0 ? `The worst case is above the ${overText(raises)}. ` : 'The worst case is above the batch budget. '}
            Raise the batch budget, or choose fewer conditions, seeds or children.
          </p>
          {raises.length > 0 && (
            <>
              <p className="text-xs text-muted-foreground">
                The worst case counts every allowed tool step and every retry of every call, so the runs make far fewer calls. A cap below the worst case can still stop the batch early. After a raise, a cap allows{' '}
                {raises.map((r) => r.spend).join(', ')}.
              </p>
              <div className="flex flex-wrap gap-2">
                {raises.map((r) => (
                  <Button key={r.cap} type="button" variant="outline" size="sm" onClick={() => onRaise?.(r.cap, r.value)}>
                    Raise {r.label} and estimate again
                  </Button>
                ))}
              </div>
            </>
          )}
        </div>
      )}
      <details>
        <summary className="cursor-pointer">Per condition and assumptions</summary>
        <div className="mt-1 overflow-x-auto">
          <table className="text-left text-xs">
            <thead className="text-muted-foreground">
              <tr>
                <th className="pr-3">Condition</th>
                <th className="pr-3">Runs</th>
                <th className="pr-3">Calls</th>
                <th className="pr-3">Input tokens</th>
                <th className="pr-3">Output tokens</th>
                <th>USD</th>
              </tr>
            </thead>
            <tbody>
              {e.per_condition.map((p) => (
                <tr key={p.condition_id}>
                  <td className="pr-3">{p.condition_id}</td>
                  <td className="pr-3">{p.runs}</td>
                  <td className="pr-3">{p.calls_max.toLocaleString()}</td>
                  <td className="pr-3">{p.input_tokens_max.toLocaleString()}</td>
                  <td className="pr-3">{p.output_tokens_max.toLocaleString()}</td>
                  <td>{money(p.usd_max)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <ul className="mt-1 list-disc pl-4 text-xs text-muted-foreground">
          {e.assumptions.map((a) => (
            <li key={a}>{a}</li>
          ))}
        </ul>
      </details>
    </div>
  )
}

/**
 * The last step before a start. A scripted experiment makes no model calls. An LLM one names its agents and the worst
 * case, and Start needs the approval box (CR-18 m1); the server's paid_calls_disabled 409 stays the final gate.
 */
function StartDialog({ e, llm, profiles, busy, onStart, onClose }: { e: ExperimentEstimate; llm: LlmAgent[]; profiles: ProviderProfile[]; busy: boolean; onStart: () => void; onClose: () => void }) {
  const [approved, setApproved] = useState(false)
  const paid = llm.length > 0
  const profileName = (id: string) => profiles.find((p) => p.id === id)?.name ?? id
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {paid ? 'Paid start: ' : ''}
            {e.runs.total} run{e.runs.total === 1 ? '' : 's'} in {e.n_cells} cell{e.n_cells === 1 ? '' : 's'}
          </DialogTitle>
          <DialogDescription>
            {paid
              ? 'These agents call their providers. Every run stops at its own budget, and the experiment starts no more runs when the batch budget is used.'
              : 'No model calls: every seller is scripted.'}
          </DialogDescription>
        </DialogHeader>
        {paid && (
          <>
            <ul aria-label="LLM agents" className="list-disc pl-4 text-sm">
              {llm.map((a) => (
                <li key={a.id}>
                  {a.id}: {profileName(a.profile)}, {a.model || 'no model'}
                </li>
              ))}
            </ul>
            <EstimateView e={e} />
            <label className="flex items-start gap-2 rounded-md border border-amber-500/50 bg-amber-500/10 p-2 text-sm">
              <input type="checkbox" className="mt-0.5 size-4" checked={approved} onChange={(ev) => setApproved(ev.target.checked)} />
              <span>
                Paid model calls: up to {e.calls_max.toLocaleString()} calls, cost {e.usd_max == null ? 'unknown' : `up to ${money(e.usd_max)}`}. I have the lead's approval.
              </span>
            </label>
          </>
        )}
        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button disabled={busy || (paid && !approved)} onClick={onStart}>
            {paid ? 'Start paid runs' : 'Start'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** One part of the branch plan: a switch, then its fields. Off sends null, so the server plans no such child. */
function PlanPart({ part, plan, set, children }: { part: keyof BranchPlan; plan: BranchPlan; set: (p: Path, v: unknown) => void; children: ReactNode }) {
  const on = plan[part] != null
  return (
    <fieldset className="space-y-2 rounded-lg border p-3">
      <legend className="px-1 text-sm font-medium">
        <label className="flex items-center gap-2">
          <input type="checkbox" className="size-4" checked={on} onChange={(e) => set(['branch_plan', part], e.target.checked ? structuredClone(DEFAULT_BRANCH_PLAN[part]) : null)} />
          {part === 'probe' ? 'Punishment probe' : part === 'placebo' ? 'Placebo children' : 'Channel ablation'}
        </label>
      </legend>
      {on && <div className="grid gap-3 sm:grid-cols-3">{children}</div>}
      <FieldError path={['branch_plan', part]} />
    </fieldset>
  )
}

function PlanNumber({ path, label, value, set, min, max }: { path: Path; label: string; value: number | undefined; set: (p: Path, v: unknown) => void; min?: number; max?: number }) {
  return (
    <FieldShell path={path} label={label}>
      <NumberInput id={fieldId(path)} step={1} min={min} max={max} value={value} onChange={(v) => set(path, v ?? undefined)} />
    </FieldShell>
  )
}

export function NewExperimentPage() {
  const navigate = useNavigate()
  const schema = useRunConfigSchema()
  const roles = useRoles()
  const tools = useTools()
  const policies = usePolicies()
  const profiles = useProfiles()
  const scenarios = useScenarios()
  const conditions = useConditions()
  const seeds = useSeeds()

  const loadError = [schema, roles, tools, policies, profiles, scenarios, conditions].find((q) => q.error)?.error
  const reg: Registries | null = useMemo(
    () =>
      schema.data && roles.data && tools.data && policies.data && profiles.data && scenarios.data
        ? { root: schema.data, roles: roles.data, tools: tools.data, policies: policies.data, profiles: profiles.data, scenarios: scenarios.data }
        : null,
    [schema.data, roles.data, tools.data, policies.data, profiles.data, scenarios.data],
  )
  const [stored, setDraft] = useState<ExperimentCreate | null>(null)
  const [errors, setErrors] = useState<{ message: string; fields: Record<string, string> } | null>(null)
  const [estimate, setEstimate] = useState<{ for: string; e: ExperimentEstimate } | null>(null)
  const [busy, setBusy] = useState(false)
  const [confirm, setConfirm] = useState(false)
  const [withDrafts, setWithDrafts] = useState(false)

  if (loadError) {
    return (
      <p role="alert" className="rounded-md border border-red-500/40 bg-red-500/10 p-3 text-sm">
        {loadError.message}
      </p>
    )
  }
  if (!reg || !conditions.data) return <Skeleton className="h-96 w-full max-w-5xl" />

  const noFrozen = !conditions.data.some((c) => c.status === 'frozen' && !changedSinceFreeze(c))
  if (noFrozen && !withDrafts) {
    // UX review 3, item 4. Pilot seeds run on draft conditions, so the empty state offers that way on, instead of removing it.
    return (
      <div className="max-w-3xl space-y-3">
        <h1 className="text-xl font-semibold">New experiment</h1>
        <div className="rounded-lg border border-dashed p-8 text-center" role="status">
          <p className="font-medium">No frozen conditions are available.</p>
          <p className="mt-1 text-sm text-muted-foreground">
            {conditions.data.length} condition{conditions.data.length === 1 ? ' is' : 's are'} still a draft. Dev and test experiments need stage 1 (conditions) of the freeze.
          </p>
          <div className="mt-3 flex flex-wrap justify-center gap-2">
            <Button asChild>
              <Link to="/settings/freeze">Go to Settings &gt; Freeze</Link>
            </Button>
            <Button variant="outline" onClick={() => setWithDrafts(true)}>
              Plan a pilot experiment on drafts
            </Button>
          </div>
        </div>
      </div>
    )
  }

  const draft = stored ?? starterExperiment(reg.root)
  const chosen = conditions.data.filter((c) => draft.conditions.includes(c.id))
  const set = (path: Path, value: unknown) => setDraft((d) => setAt(d ?? draft, path, value))
  const groups = [...new Set(conditions.data.map((c) => c.group))]
  const request = experimentRequest(draft, chosen, reg.root, groups)
  const json = JSON.stringify(request)
  const fresh = estimate?.for === json ? estimate.e : null
  const plan = draft.branch_plan ?? {}
  const grid = planGrid(chosen, draft.seeds, seeds.data?.seeds, plan)
  const nRuns = grid.flat().reduce((n, c) => n + runsOf(c), 0)
  const devBlocked = chosen.map(devRefusal).filter(Boolean)
  const pool = (draft.split === 'pilot' || draft.split === 'dev' || draft.split === 'test' ? seeds.data?.[draft.split] : undefined) ?? []
  const sellerIds = draft.agents.filter((a) => a.role === 'seller').map((a) => a.id)

  const ctx: FormCtx = {
    root: reg.root,
    research: true,
    errors: errors?.fields ?? {},
    set,
    lockedValue: (p) => {
      if (p[0] === 'memory' && p[1] === 'summary') return chosen[0]?.memory_summary
      if (p.includes('system_prompt') || p.includes('talk_prompt')) return 'Set by each condition and seed (see Conditions).'
      return undefined
    },
  }

  const fail = (e: unknown) => {
    const a = asApiError(e)
    if (NO_CAP_RE.test(a.message)) return setErrors({ message: NO_CAP_TEXT, fields: {} })
    // One path can carry an error per condition (branch_plan.placebo for C1 and C2a), so messages are joined, not replaced.
    const fields: Record<string, string> = {}
    for (const f of a.fields) fields[f.path] = fields[f.path] ? `${fields[f.path]}; ${f.message}` : f.message
    setErrors({ message: a.message, fields })
  }
  async function runEstimate(req: ExperimentCreate = request) {
    setBusy(true)
    setErrors(null)
    try {
      setEstimate({ for: JSON.stringify(req), e: await estimateExperiment(req) })
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
      const x = await startExperiment(request)
      navigate(`/experiments/${x.id}`)
    } catch (e) {
      setConfirm(false)
      fail(e)
    } finally {
      setBusy(false)
    }
  }

  const toggleCondition = (id: string, on: boolean) => {
    const next = conditions.data!.filter((c) => (c.id === id ? on : draft.conditions.includes(c.id)))
    const d = withConditions(draft, next, reg)
    setDraft(draft.split === 'dev' && next.some(devRefusal) ? { ...d, split: 'pilot', seeds: [] } : d)
  }

  return (
    <Form.Provider value={ctx}>
      <form className="max-w-5xl space-y-4 pb-24" onSubmit={(e) => e.preventDefault()} noValidate>
        <div>
          <h1 className="text-xl font-semibold">New experiment</h1>
          <p className="text-sm text-muted-foreground">
            Conditions × seeds. Each cell is one research run; when it completes, the server branches the children of the plan from it. The conditions set labels, channels, prompts and tools.
          </p>
        </div>

        <Section title="Cells">
          <FieldShell path={['name']} label="Name">
            <Input id={fieldId(['name'])} value={draft.name ?? ''} maxLength={120} onChange={(e) => set(['name'], e.target.value || null)} />
          </FieldShell>
          <fieldset className="space-y-1">
            <legend className="text-sm font-medium">Conditions (rows)</legend>
            {conditions.data.map((c) => (
              <label key={c.id} className="flex items-start gap-2 text-sm">
                <input type="checkbox" className="mt-0.5 size-4" disabled={changedSinceFreeze(c)} checked={draft.conditions.includes(c.id)} onChange={(e) => toggleCondition(c.id, e.target.checked)} />
                <span>
                  <span className="font-medium">{c.id}</span> {c.name}{' '}
                  <span className="text-xs text-muted-foreground">
                    (legal {c.labels.legal}, harm {c.labels.harm}, {c.status}
                    {changedSinceFreeze(c) ? '; changed since the freeze, the server refuses it' : ''})
                  </span>
                </span>
              </label>
            ))}
            {Object.entries(errors?.fields ?? {})
              .filter(([k]) => k.startsWith('conditions'))
              .map(([k, m]) => (
                <p key={k} role="alert" className="text-xs text-red-600 dark:text-red-400">
                  {draft.conditions[Number(k.split('.')[1])] ?? k}: {m}
                </p>
              ))}
          </fieldset>
          <FieldShell path={['split']} label="Split">
            <NativeSelect id={fieldId(['split'])} value={draft.split} onChange={(e) => setDraft({ ...draft, split: e.target.value as ExperimentCreate['split'], seeds: [] })}>
              <option value="pilot">pilot</option>
              <option value="dev" disabled={devBlocked.length > 0}>
                dev{devBlocked.length ? ` (${devBlocked.join('; ')})` : ''}
              </option>
              <option value="test" disabled={seeds.data?.test_locked !== false}>
                test{seeds.data?.test_locked !== false ? ' (locked until the freeze)' : ''}
              </option>
            </NativeSelect>
          </FieldShell>
          <fieldset className="space-y-2">
            <legend className="text-sm font-medium">Seeds (columns, {draft.seeds.length} selected)</legend>
            <div className="flex flex-wrap gap-1.5">
              {pool.map((s) => {
                const info = seeds.data?.seeds?.find((x) => x.seed === s)
                return (
                  <label key={s} className="flex items-center gap-1 rounded-md border px-2 py-1 font-mono text-xs" title={info ? `${info.scenario_id}, news ${info.shock_news}` : undefined}>
                    <input
                      type="checkbox"
                      className="size-3.5"
                      checked={draft.seeds.includes(s)}
                      onChange={(e) => set(['seeds'], e.target.checked ? [...draft.seeds, s].sort((a, b) => a - b) : draft.seeds.filter((x) => x !== s))}
                    />
                    {s}
                  </label>
                )
              })}
            </div>
            <Button type="button" variant="ghost" size="sm" onClick={() => set(['seeds'], pool)}>
              Select all {pool.length}
            </Button>
            {seeds.error && <p role="alert" className="text-xs text-red-600 dark:text-red-400">{seeds.error.message}</p>}
            <FieldError path={['seeds']} />
          </fieldset>
        </Section>

        {chosen.length > 0 && (
          <Section title="Agents">
            <p className="text-xs text-muted-foreground">The same agents play every condition, unless a condition gets its own below. Research runs use the scripted host.</p>
            <AgentsEditor agents={draft.agents} reg={reg} />
            {chosen.map((c) => {
              const own = draft.agents_by_condition?.[c.id]
              const note = sameModelNote({ agents: own ?? draft.agents }, c)
              return (
                <div key={c.id} className="space-y-2 rounded-lg border p-3">
                  <label className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      className="size-4"
                      checked={own != null}
                      onChange={(e) => {
                        const by = { ...draft.agents_by_condition }
                        if (e.target.checked) by[c.id] = structuredClone(draft.agents)
                        else delete by[c.id]
                        set(['agents_by_condition'], by)
                      }}
                    />
                    Other agents for {c.id}
                  </label>
                  {own && <AgentsEditor agents={own} path={['agents_by_condition', c.id]} reg={reg} condition={c} />}
                  {note && <p className="text-sm text-amber-700 dark:text-amber-400">{note}</p>}
                </div>
              )
            })}
          </Section>
        )}

        <Section title="Branch plan">
          <p className="text-xs text-muted-foreground">Children start when their parent has completed. The defaults are the plan's (§11).</p>
          <PlanPart part="probe" plan={plan} set={set}>
            <PlanNumber path={['branch_plan', 'probe', 'at']} label="Fork round" value={plan.probe?.at} set={set} min={2} />
            <PlanNumber path={['branch_plan', 'probe', 'rounds']} label="Rounds" value={plan.probe?.rounds} set={set} min={1} />
            <FieldShell path={['branch_plan', 'probe', 'target']} label="Target seller">
              <NativeSelect id={fieldId(['branch_plan', 'probe', 'target'])} value={plan.probe?.target ?? ''} onChange={(e) => set(['branch_plan', 'probe', 'target'], e.target.value)}>
                {!sellerIds.includes(plan.probe?.target ?? '') && <option value={plan.probe?.target ?? ''}>{plan.probe?.target || 'Select…'}</option>}
                {sellerIds.map((id) => (
                  <option key={id} value={id}>
                    {id}
                  </option>
                ))}
              </NativeSelect>
            </FieldShell>
          </PlanPart>
          <PlanPart part="placebo" plan={plan} set={set}>
            <PlanNumber path={['branch_plan', 'placebo', 'at']} label="Fork round" value={plan.placebo?.at} set={set} min={2} />
            <PlanNumber path={['branch_plan', 'placebo', 'rounds']} label="Rounds" value={plan.placebo?.rounds} set={set} min={1} />
            <PlanNumber path={['branch_plan', 'placebo', 'n']} label="Children" value={plan.placebo?.n} set={set} min={1} max={10} />
          </PlanPart>
          <PlanPart part="ablation" plan={plan} set={set}>
            <PlanNumber path={['branch_plan', 'ablation', 'at']} label="From round" value={plan.ablation?.at} set={set} min={2} />
            <FieldShell path={['branch_plan', 'ablation', 'mode']} label="Messages">
              <NativeSelect id={fieldId(['branch_plan', 'ablation', 'mode'])} value={plan.ablation?.mode ?? 'shuffle'} onChange={(e) => set(['branch_plan', 'ablation', 'mode'], e.target.value)}>
                <option value="shuffle">shuffle</option>
                <option value="clear">clear</option>
              </NativeSelect>
            </FieldShell>
            <fieldset className="space-y-1">
              <legend className="text-sm font-medium">Groups</legend>
              {groups.map((g) => (
                <label key={g} className="mr-3 inline-flex items-center gap-1 text-sm">
                  <input
                    type="checkbox"
                    className="size-4"
                    checked={(plan.ablation?.groups ?? []).includes(g)}
                    onChange={(e) => set(['branch_plan', 'ablation', 'groups'], e.target.checked ? [...(plan.ablation?.groups ?? []), g] : (plan.ablation?.groups ?? []).filter((x) => x !== g))}
                  />
                  {g}
                </label>
              ))}
            </fieldset>
          </PlanPart>
        </Section>

        <Section title="Market">
          <SchemaField path={['opening_price']} name="opening_price" schema={OPENING_PRICE_SCHEMA} value={draft.opening_price} />
        </Section>

        <Section title="Budgets, clock and memory">
          <SchemaField path={['budget']} name="budget" schema={{ $ref: '#/$defs/BudgetSettings', title: 'Budget per run (every parent and every child)' }} value={draft.budget} required />
          <SchemaField path={['batch_budget']} name="batch_budget" schema={{ $ref: '#/$defs/BatchBudget', title: 'Batch budget (shared by every run)' }} value={draft.batch_budget} required />
          <SchemaField path={['clock']} name="clock" schema={{ $ref: '#/$defs/ClockSettings', title: 'Clock' }} value={draft.clock} />
          <SchemaField path={['memory']} name="memory" schema={{ $ref: '#/$defs/MemorySettings', title: 'Memory' }} value={draft.memory} />
        </Section>

        <Section title={`Plan: ${count(grid.flat().length, 'cell')}, ${count(nRuns, 'run')}`}>
          {grid.length === 0 || draft.seeds.length === 0 ? (
            <p className="text-sm text-muted-foreground">Choose at least one condition and one seed.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="text-left text-xs" aria-label="Planned cells">
                <thead className="text-muted-foreground">
                  <tr>
                    <th className="p-1.5">Condition</th>
                    {draft.seeds.map((s) => (
                      <th key={s} className="p-1.5 font-mono">
                        {s}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {grid.map((row) => (
                    <tr key={row[0]!.condition.id} className="border-t align-top">
                      <th scope="row" className="max-w-56 p-1.5 font-normal">
                        <span className="font-medium">{row[0]!.condition.id}</span>
                        <span className="block text-muted-foreground">{describeChildren(row[0]!.children)}</span>
                      </th>
                      {row.map((cell) => (
                        <td key={cell.seed} className="p-1.5 whitespace-nowrap">
                          <span className="block">{cell.variant}</span>
                          <span className="block font-mono text-muted-foreground">{cell.info?.scenario_id ?? '?'}</span>
                          <span className="block text-muted-foreground">{cell.info ? (cell.info.shock_news === 'private' ? `news to ${cell.info.informed_seller}` : 'news public') : ''}</span>
                          <span className="block">{runsOf(cell)} runs</span>
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Section>

        <div className="sticky bottom-0 z-10 -mx-4 space-y-2 border-t bg-background/95 px-4 py-3 backdrop-blur">
          {errors && (
            <div role="alert" className="text-sm text-red-600 dark:text-red-400">
              <p>{errors.message}</p>
              {Object.keys(errors.fields).length > 0 && (
                <ul className="list-disc pl-4 text-xs">
                  {Object.entries(errors.fields).map(([k, m]) => (
                    <li key={k}>
                      {k || 'request'}: {m}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
          {fresh && (
            <EstimateView
              e={fresh}
              batch={draft.batch_budget}
              onRaise={(cap, value) => {
                const next = setAt(draft, ['batch_budget', cap], value) as ExperimentCreate
                setDraft(next)
                return runEstimate(experimentRequest(next, chosen, reg.root, groups))
              }}
            />
          )}
          {estimate && !fresh && <p className="text-xs text-muted-foreground">The form changed after the estimate. Estimate again before you start.</p>}
          <div className="flex flex-wrap gap-2">
            <Button type="button" variant="outline" disabled={busy || !hasCap(draft.budget)} title={hasCap(draft.budget) ? undefined : NO_CAP_TEXT} onClick={() => runEstimate()}>
              <Calculator /> Estimate cost
            </Button>
            <Button type="button" disabled={busy || !fresh || fresh.within_budget === false} onClick={() => setConfirm(true)} title={fresh ? undefined : 'Estimate the cost first.'}>
              <Play /> Start experiment
            </Button>
          </div>
        </div>
      </form>

      {confirm && fresh && <StartDialog e={fresh} llm={llmAgents(request)} profiles={reg.profiles} busy={busy} onStart={start} onClose={() => setConfirm(false)} />}
    </Form.Provider>
  )
}

