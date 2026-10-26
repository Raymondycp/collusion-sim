import type { BranchPlan, ExperimentCell, ExperimentSummary } from '@/api/experiments'
import type { RunSummary } from '@/api/types'
import type { TrailLine } from '@/api/trail'

export const PURPOSE_TEXT: Record<TrailLine['purpose'], string> = { talk: 'Talk call', act: 'Pricing call', one_call: 'Single call', summary: 'Summary call' }

export const short = (id: string) => (id.length > 14 ? `…${id.slice(-8)}` : id)

/**
 * The experiment of a run, as a label: its own experiment_id, else its parent's (contract 0.22.0). A branch that a user
 * starts from an experiment run has no experiment_id of its own. No label when the server does not send the field.
 */
export function experimentLabel(run: RunSummary, runs: RunSummary[], experiments: ExperimentSummary[]): string | null {
  const parent = run.experiment_id == null && run.branch ? runs.find((r) => r.id === run.branch!.parent_run_id) : undefined
  const id = run.experiment_id ?? parent?.experiment_id
  if (!id) return null
  const name = experiments.find((e) => e.id === id)?.name ?? short(id)
  return run.experiment_id ? `in ${name}` : `branch of a run in ${name}`
}

/**
 * The condition of a run for people. An ablation child has no condition of its own, because the ablation changes the
 * condition (lead, A7 finding 11). It shows its parent's condition and "ablated". Null when neither has one.
 */
export function conditionText(run: Pick<RunSummary, 'condition_id' | 'branch'>, parent: Pick<RunSummary, 'condition_id'> | undefined): string | null {
  if (run.condition_id) return run.condition_id
  return run.branch?.intervention.type === 'ablation' && parent?.condition_id ? `${parent.condition_id} ablated` : null
}

type PlannedBranch = ExperimentCell['branches'][number]

/**
 * A planned child for people: its fork round and what it changes, not its plan index (10 × position + sample).
 * A condition with no private channel gets a null ablation child with no mode (CR-63 3), so its label names no mode.
 */
export function branchLabel(b: Pick<PlannedBranch, 'kind' | 'index'>, plan: BranchPlan | undefined, { privateChannel = true }: { privateChannel?: boolean } = {}): string {
  const sample = b.index % 10
  const at = (r: number | undefined) => (r === undefined ? '' : ` at R${r}`)
  if (b.kind === 'probe') return `probe${plan?.probe?.target ? ` ${plan.probe.target}` : ''}${at(plan?.probe?.at)}`
  if (b.kind === 'placebo') return `placebo ${sample + 1}${plan?.placebo?.n ? ` of ${plan.placebo.n}` : ''}${at(plan?.placebo?.at)}`
  const from = plan?.ablation?.at === undefined ? '' : ` from R${plan.ablation.at}`
  return `ablation${privateChannel && plan?.ablation?.mode ? ` (${plan.ablation.mode})` : ''}${sample > 0 ? ` ${sample + 1}` : ''}${from}`
}
