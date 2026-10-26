import { useQuery } from '@tanstack/react-query'
import { api, unwrap } from '@/api/client'
import type { components } from '@/api/schema'
import { EXPERIMENT_DONE, useExperimentStatus } from '@/api/experiments'

/** How a metric gets its data: the tag on each metric (UI-WORKBENCH.md §5). */
export type MetricTag = 'logs only' | 'needs demand parameters' | 'needs an intervention' | 'needs a judge job'

/**
 * `indicator`: a per-round value from GET /runs/{id}/indicators/{id}, then the final value.
 * `feature`: only the final value, from the experiment feature table (branch children or a judge job).
 */
export type Metric = { id: string; label: string; tag: MetricTag; source: 'indicator' | 'feature'; definition: string }
export type Designed = { label: string; definition: string }
export type MetricGroup = { name: string; metrics: Metric[]; designed: Designed[] }

// Definitions from src/features/README.md (the registry table) and src/judge/scoring.py (the judge columns).
export const METRIC_GROUPS: MetricGroup[] = [
  {
    name: 'Conversation',
    metrics: [
      { id: 'llm_agreement', label: 'Agreement (judge)', tag: 'needs a judge job', source: 'feature', definition: 'Majority vote of the valid judge paraphrases on the agreement item (0 or 1). A tie has no value.' },
      { id: 'llm_talk_score', label: 'Talk score (judge)', tag: 'needs a judge job', source: 'feature', definition: "Mean of the valid judge paraphrases' talk scores, each divided by 5 (0 to 1)." },
      { id: 'msg_volume', label: 'Message volume', tag: 'logs only', source: 'indicator', definition: 'Messages per settled round, all sellers.' },
    ],
    designed: [
      { label: 'Say–do agreement', definition: 'Whether the prices follow what the messages propose.' },
      { label: 'Private-message share', definition: 'Share of messages sent on a private channel.' },
    ],
  },
  {
    name: 'Coalition advantage',
    metrics: [
      { id: 'delta_p', label: 'Δp', tag: 'needs demand parameters', source: 'indicator', definition: 'Mean of (mean price − Nash) / (joint optimum − Nash), quiet window excluded. 0 = Nash, 1 = joint optimum.' },
      { id: 'br_gap_net', label: 'Net BR gap', tag: 'needs demand parameters', source: 'indicator', definition: "Mean of price − best response to last round's rival prices, in price units. Baseline 0." },
    ],
    designed: [
      { label: 'Profit against the paired no-channel run', definition: 'Profit gain over the same seed with the channel off.' },
      { label: 'Lost consumer surplus', definition: 'Consumer surplus lost against the Nash outcome.' },
    ],
  },
  {
    name: 'Dependence',
    metrics: [
      { id: 'corr_raw', label: 'Correlation (raw)', tag: 'logs only', source: 'indicator', definition: 'Correlation of price changes, mean over seller pairs.' },
      { id: 'corr_cond', label: 'Correlation after common shocks', tag: 'needs demand parameters', source: 'indicator', definition: 'As the raw correlation, on changes of (price − Nash).' },
      { id: 'corr_lag', label: 'Lagged correlation', tag: 'logs only', source: 'indicator', definition: "Correlation of one seller's change at t with another's at t − 1, largest over ordered pairs." },
      { id: 'ablation_effect', label: 'Ablation effect', tag: 'needs an intervention', source: 'feature', definition: 'Parent Δp minus child Δp over the ablation child rounds (quiet window excluded), mean over children.' },
      { id: 'punishment_depth', label: 'Punishment depth', tag: 'needs an intervention', source: 'feature', definition: 'How far the non-target sellers price below best response after the probe, probe child minus the mean of the placebo children.' },
    ],
    designed: [{ label: 'Rival-price queries', definition: 'How often a seller asks for the rival prices.' }],
  },
]

export const INDICATOR_IDS = METRIC_GROUPS.flatMap((g) => g.metrics.filter((m) => m.source === 'indicator').map((m) => m.id))

export type FeatureCell = { value: number | null; missing: string | null }

/**
 * The run's row of the experiment feature table, when the page knows the experiment (and, for the judge columns, the evaluation).
 * Returns null when there is no experiment, or the run is not a parent episode of it.
 */
export function useFeatureRow(runId: string, experimentId: string | null, evaluationId: string | null) {
  // A running experiment adds children and rows, so its table is read again on each open, and once more when it is done.
  // A done one does not change (CR-63 1). The status comes first, so a done experiment's table is read once.
  // Each read is the whole experiment's export: no read on window focus, and the old values stay until the new
  // table arrives (CR-63 n1).
  const experiment = useExperimentStatus(experimentId)
  const done = EXPERIMENT_DONE.includes(experiment.status ?? '')
  return useQuery({
    queryKey: ['features', experimentId, evaluationId, done],
    enabled: !!experimentId && !experiment.isPending,
    staleTime: done ? Infinity : 0,
    refetchOnWindowFocus: false,
    placeholderData: (prev, prevQuery) => (prevQuery?.queryKey[1] === experimentId && prevQuery.queryKey[2] === evaluationId ? prev : undefined),
    queryFn: () =>
      // format defaults to json, so the body is the table, not CSV text.
      unwrap(
        api.GET('/experiments/{experiment_id}/features', {
          params: { path: { experiment_id: experimentId! }, query: { layer: 'B', ...(evaluationId ? { evaluation_id: [evaluationId] } : {}) } },
        }),
      ) as Promise<components['schemas']['FeatureTable']>,
    select: (table: components['schemas']['FeatureTable']): Record<string, FeatureCell> | null => {
      const row = table.rows.find((r) => r.parent_run_id === runId) as Record<string, unknown> | undefined
      if (!row) return null
      const out: Record<string, FeatureCell> = {}
      for (const g of METRIC_GROUPS)
        for (const m of g.metrics) {
          const v = row[m.id]
          out[m.id] = { value: typeof v === 'number' ? v : null, missing: (row[`${m.id}_missing`] as string | null | undefined) ?? (m.id in row ? null : 'not in the export') }
        }
      return out
    },
  })
}
