import { useQuery } from '@tanstack/react-query'
import { api, unwrap } from './client'
import type { components } from './schema'

type S = components['schemas']
export type ExperimentCreate = S['ExperimentCreate']
export type BranchPlan = S['BranchPlan']
export type BatchBudget = S['BatchBudget']
export type ExperimentEstimate = S['ExperimentEstimate']
export type ExperimentSummary = S['ExperimentSummary']
export type Experiment = S['Experiment']
export type ExperimentCell = S['ExperimentCell']
export type CellStatus = ExperimentCell['status']
export type BranchKind = ExperimentCell['branches'][number]['kind']

/** The contract's defaults for BranchPlan (ProbePlan, PlaceboPlan, AblationPlan). A test checks them against openapi.yaml. */
export const DEFAULT_BRANCH_PLAN = {
  probe: { at: 20, rounds: 5, target: 'B' },
  placebo: { at: 20, rounds: 5, n: 3 },
  ablation: { at: 10, mode: 'shuffle', groups: ['C1', 'C2a', 'C2b', 'C3', 'C4', 'LC'] },
} satisfies Required<BranchPlan>

/** Cell and run states after which nothing more happens to them. */
export const ENDED: readonly string[] = ['completed', 'stopped', 'failed']

/**
 * The list polls while it holds an experiment that can still change, so a page sees the change to done. A blocked one
 * changes only when another run ends or on Retry, and it can stay blocked for hours, so it polls slower (CR-63 n3).
 */
const listPoll = (list: { status: string }[] | undefined) =>
  list?.some((e) => e.status === 'running') ? 5_000 : list?.some((e) => e.status === 'blocked') ? 15_000 : false

/** `includeArchived` also lists archived experiments (DECISIONS-29). Its cache key sits under ['experiments'], so every invalidation reaches it. */
export const useExperiments = (includeArchived = false) =>
  useQuery({
    queryKey: includeArchived ? ['experiments', 'archived'] : ['experiments'],
    queryFn: () => unwrap(api.GET('/experiments', includeArchived ? { params: { query: { include_archived: true } } } : undefined)),
    refetchInterval: (q) => listPoll(q.state.data),
  })

/**
 * An experiment's status from the summary list, not from its detail with every cell (CR-63 n2). `status` is undefined
 * when there is no id, while the list loads, or when the list does not hold the id.
 */
export function useExperimentStatus(id: string | null) {
  const list = useExperiments()
  return { status: id ? list.data?.find((e) => e.id === id)?.status : undefined, isPending: !!id && list.isPending }
}

/** Experiment states after which no cell runs again. Retry starts only blocked cells, and a blocked experiment is not done. */
export const EXPERIMENT_DONE: readonly string[] = ['completed', 'ended']

/** Polls while the experiment has live cells. A blocked one changes only when another run ends or on Retry, so it polls slowly. */
export const useExperiment = (id: string) =>
  useQuery({
    queryKey: ['experiment', id],
    queryFn: () => unwrap(api.GET('/experiments/{experiment_id}', { params: { path: { experiment_id: id } } })),
    refetchInterval: (q) => (q.state.data?.status === 'running' ? 3000 : q.state.data?.status === 'blocked' ? 15000 : false),
  })

export const estimateExperiment = (body: ExperimentCreate) => unwrap(api.POST('/experiments/estimate', { body }))
export const startExperiment = (body: ExperimentCreate) => unwrap(api.POST('/experiments', { body }))
/** Forgets every block and advances the experiment. A cell whose cause is still there is blocked again with a new error.at. */
export const retryExperiment = (id: string) => unwrap(api.POST('/experiments/{experiment_id}/retry', { params: { path: { experiment_id: id } } }))
