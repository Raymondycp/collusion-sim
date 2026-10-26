import { DEFAULT_BRANCH_PLAN, type BranchKind, type BranchPlan, type ExperimentCreate } from '@/api/experiments'
import { variantFor, type AgentConfig, type Condition, type RunConfig, type SeedSplits } from '@/api/runConfig'
import { clearLocked, conditionSellers, type Registries } from '@/features/new-run/draft'
import { defaultFor, type JSONSchema } from '@/lib/jsonSchema'

export type SeedInfo = NonNullable<SeedSplits['seeds']>[number]

/** A child run that the server will branch from a completed parent. `rounds` null = to the parent's last round. */
export type PlannedChild = { kind: BranchKind; at: number; rounds: number | null; n: number; channel?: string | null }

/** The children of one cell, as the backend plans them (services/experiments.py `_branch_requests`).
 *  A condition with no private channel gets one null ablation child (contract 0.10.0). */
export function plannedChildren(plan: BranchPlan, c: Condition): PlannedChild[] {
  const out: PlannedChild[] = []
  if (plan.probe) out.push({ kind: 'probe', at: plan.probe.at ?? 20, rounds: plan.probe.rounds ?? 5, n: 1 })
  if (plan.placebo) out.push({ kind: 'placebo', at: plan.placebo.at ?? 20, rounds: plan.placebo.rounds ?? 5, n: plan.placebo.n ?? 3 })
  const groups = plan.ablation?.groups ?? DEFAULT_BRANCH_PLAN.ablation.groups
  if (plan.ablation && groups.includes(c.group) && !c.channels.private_pairs) {
    out.push({ kind: 'ablation', at: plan.ablation.at ?? 10, rounds: null, n: 1, channel: null })
  } else if (plan.ablation && groups.includes(c.group)) {
    const sellers = Object.keys(c.capabilities).sort()
    sellers.forEach((x, i) =>
      sellers.slice(i + 1).forEach((y) => out.push({ kind: 'ablation', at: plan.ablation!.at ?? 10, rounds: null, n: 1, channel: `private_${x}_${y}` })),
    )
  }
  return out
}

export type PlanCell = { condition: Condition; seed: number; variant: string; info: SeedInfo | undefined; children: PlannedChild[] }

/** Rows = conditions, columns = seeds (DECISIONS-10 §3). */
export function planGrid(conditions: Condition[], seeds: number[], seedInfo: SeedInfo[] | undefined, plan: BranchPlan): PlanCell[][] {
  return conditions.map((c) => {
    const children = plannedChildren(plan, c)
    return seeds.map((seed) => ({ condition: c, seed, variant: variantFor(c, seed).id, info: seedInfo?.find((s) => s.seed === seed), children }))
  })
}

export const runsOf = (cell: Pick<PlanCell, 'children'>) => 1 + cell.children.reduce((n, c) => n + c.n, 0)

export function describeChildren(children: PlannedChild[]): string {
  if (children.length === 0) return 'no children'
  return children
    .map((c) => (c.kind === 'ablation' ? (c.channel ? `ablation of ${c.channel} from R${c.at}` : `null ablation from R${c.at}`) : `${c.n} ${c.kind} at R${c.at} for ${c.rounds}`))
    .join(', ')
}

/** A new experiment: nothing chosen, the contract's branch plan, and the schema's defaults for clock and memory. */
export function starterExperiment(root: JSONSchema): ExperimentCreate {
  const def = (name: string) => (root.$defs?.[name] ? defaultFor(root, root.$defs[name]!) : undefined) as never
  return {
    name: null,
    conditions: [],
    split: 'pilot',
    seeds: [],
    agents: [],
    clock: def('ClockSettings'),
    memory: def('MemorySettings'),
    budget: {} as never,
    batch_budget: {},
    branch_plan: structuredClone(DEFAULT_BRANCH_PLAN),
  }
}

/** The sellers that the chosen conditions name, keeping each one's policy. Overrides of conditions no longer chosen are dropped. */
export function withConditions(d: ExperimentCreate, chosen: Condition[], r: Registries): ExperimentCreate {
  const ids = [...new Set(chosen.flatMap((c) => Object.keys(c.capabilities)))].sort()
  const by = Object.fromEntries(Object.entries(d.agents_by_condition ?? {}).filter(([cid]) => chosen.some((c) => c.id === cid)))
  return { ...d, conditions: chosen.map((c) => c.id), agents: conditionSellers(ids, d.agents, r), agents_by_condition: by }
}

export type LlmAgent = { id: string; profile: string; model: string }

/** The LLM agents of a request, in `agents` and in every `agents_by_condition` list (`C2a/A`), as the CLI's `llm_agents`. */
export function llmAgents(d: Pick<ExperimentCreate, 'agents' | 'agents_by_condition'>): LlmAgent[] {
  const lists: [string, AgentConfig[]][] = [['', d.agents], ...Object.entries(d.agents_by_condition ?? {}).map(([cid, a]): [string, AgentConfig[]] => [`${cid}/`, a])]
  return lists.flatMap(([prefix, agents]) =>
    agents.filter((a) => a.policy.kind === 'llm').map((a) => ({ id: `${prefix}${a.id}`, profile: a.policy.llm?.provider_profile_id ?? '', model: a.policy.llm?.model ?? '' })),
  )
}

const cleanAgents =(agents: AgentConfig[], root: JSONSchema) => clearLocked({ agents } as RunConfig, undefined, root).agents

/**
 * The body the server takes. Agent fields that a condition sets (capability, prompts) are null in every agent list,
 * memory.summary is the conditions' memory note (the server refuses any other), and the ablation groups are the
 * ticked ones among `shownGroups` (the contract's default also names groups that have no condition file, such as C4 and LC).
 */
export function experimentRequest(d: ExperimentCreate, chosen: Condition[], root: JSONSchema, shownGroups: string[]): ExperimentCreate {
  const by = Object.entries(d.agents_by_condition ?? {})
  const summary = chosen[0]?.memory_summary
  const ablation = d.branch_plan?.ablation
  return {
    ...d,
    ...(ablation ? { branch_plan: { ...d.branch_plan, ablation: { ...ablation, groups: (ablation.groups ?? []).filter((g) => shownGroups.includes(g)) } } } : {}),
    agents: cleanAgents(d.agents, root),
    agents_by_condition: Object.fromEntries(by.map(([cid, a]) => [cid, cleanAgents(a, root)])),
    ...(d.memory && summary ? { memory: { ...d.memory, summary } } : {}),
  }
}
