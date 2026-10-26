import { useQuery } from '@tanstack/react-query'
import { api, ApiError, unwrap } from './client'
import { EXPERIMENT_DONE, useExperimentStatus } from './experiments'
import type { RunKind, RunStatus, Split } from './types'
import type { components } from './schema'

export type RunFilters = {
  kind?: RunKind
  status?: RunStatus
  split?: Split
  condition_id?: string
  scenario_id?: string
  preset_id?: string
  model?: string
  created_from?: string
  created_to?: string
  /** Also list archived runs (DECISIONS-29, contract 0.39.0). */
  include_archived?: boolean
}

export const useHealth = () =>
  useQuery({ queryKey: ['health'], queryFn: () => unwrap(api.GET('/health')), refetchInterval: 15_000, retry: false })

/** GET /runs has no parents-only filter, so a page that drops branch children can list fewer runs than this. */
export const RUNS_LIMIT = 200

export const useRuns = (filters: RunFilters) =>
  useQuery({
    queryKey: ['runs', filters],
    queryFn: () => unwrap(api.GET('/runs', { params: { query: { ...filters, limit: RUNS_LIMIT } } })),
    refetchInterval: 5_000,
  })

export const useRun = (runId: string, { enabled = true }: { enabled?: boolean } = {}) =>
  useQuery({
    queryKey: ['run', runId],
    enabled,
    queryFn: () => unwrap(api.GET('/runs/{run_id}', { params: { path: { run_id: runId } } })),
    // A missing run (404) stops the polling. Another error (a network fault) keeps it, so a live run recovers.
    refetchInterval: (q) => ((q.state.error instanceof ApiError && q.state.error.code === 'not_found') || (q.state.data && ['completed', 'stopped', 'failed'].includes(q.state.data.status)) ? false : 3_000),
  })

const ENDED = ['completed', 'stopped', 'failed']

/**
 * The child runs of a run. The poll stops when the run and every child have ended and no experiment can add a child:
 * an experiment adds children to its parent runs after they end, never to a branch run, and none once it is done
 * (CR-63 2). A new branch from the branch dialog refreshes the list itself.
 */
export const useBranches = (runId: string) => {
  const run = useRun(runId).data
  const parentInExperiment = !!run?.experiment_id && !run.branch
  const experiment = useExperimentStatus(parentInExperiment ? run.experiment_id! : null)
  const noNewChild = !parentInExperiment || EXPERIMENT_DONE.includes(experiment.status ?? '')
  return useQuery({
    queryKey: ['branches', runId],
    queryFn: () => unwrap(api.GET('/runs/{run_id}/branches', { params: { path: { run_id: runId } } })),
    refetchInterval: (q) => (run && ENDED.includes(run.status) && noNewChild && q.state.data?.every((c) => ENDED.includes(c.status)) ? false : 5_000),
  })
}

export const useObservation = (runId: string, agentId: string | null, t: number | null) =>
  useQuery({
    queryKey: ['observation', runId, agentId, t],
    enabled: agentId !== null && t !== null && t > 0,
    queryFn: () =>
      unwrap(api.GET('/runs/{run_id}/observations', { params: { path: { run_id: runId }, query: { agent_id: agentId!, t: t! } } })),
    staleTime: Infinity,
  })

export const createBranches = (runId: string, body: components['schemas']['BranchRequest']) =>
  unwrap(api.POST('/runs/{run_id}/branches', { params: { path: { run_id: runId } }, body }))

export type CloneRequest = components['schemas']['CloneRequest']
export const cloneRun = (runId: string, body: CloneRequest) => unwrap(api.POST('/runs/{run_id}/clone', { params: { path: { run_id: runId } }, body }))

export const useCompare = (ids: string[]) =>
  useQuery({
    queryKey: ['compare', ids],
    enabled: ids.length >= 2,
    queryFn: () => unwrap(api.GET('/runs/compare', { params: { query: { ids: ids.join(',') } } })),
  })
