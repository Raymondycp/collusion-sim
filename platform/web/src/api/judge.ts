import { useQuery } from '@tanstack/react-query'
import { api, unwrap } from './client'
import type { components } from './schema'

type S = components['schemas']
export type JudgeView = S['JudgeView']
export type JudgeBlock = S['JudgeBlock']
export type JudgeItemSet = S['JudgeItemSet']
export type Transcript = S['Transcript']
export type ViewId = JudgeView['id']
export type TransformId = Transcript['transform']
export type Evaluation = S['Evaluation']

export const TRANSFORMS: { id: TransformId; name: string }[] = [
  { id: 'none', name: 'None' },
  { id: 'round_shuffle', name: 'Round shuffle (robustness block)' },
]

export const useJudgeViews = () => useQuery({ queryKey: ['judge', 'views'], queryFn: () => unwrap(api.GET('/judge/views')), staleTime: Infinity })
export const useJudgeBlocks = () => useQuery({ queryKey: ['judge', 'blocks'], queryFn: () => unwrap(api.GET('/judge/blocks')), staleTime: Infinity })
export const useJudgeItemSets = () => useQuery({ queryKey: ['judge', 'item-sets'], queryFn: () => unwrap(api.GET('/judge/item-sets')) })
/**
 * The completed judge jobs with an LLM backend that judged a parent of the experiment, newest first. GET /evaluations
 * has no experiment filter, so the parent run ids decide. The feature export joins only LLM scores and refuses another
 * job with 422 (CR-65 finding 2, CR-67 n1).
 */
export const useExperimentEvaluations = (parentRunIds: string[]) =>
  useQuery({
    queryKey: ['evaluations', 'completed'],
    queryFn: () => unwrap(api.GET('/evaluations', { params: { query: { status: 'completed' } } })),
    select: (all: Evaluation[]) => all.filter((e) => !!e.backends.llm && e.run_ids.some((r) => parentRunIds.includes(r))),
  })

export const useTranscript = (runId: string | null, view: ViewId, transform: TransformId) =>
  useQuery({
    queryKey: ['transcript', runId, view, transform],
    queryFn: () => unwrap(api.GET('/runs/{run_id}/transcript', { params: { path: { run_id: runId! }, query: { view, transform } } })),
    enabled: !!runId,
  })
