import type { components } from '@/api/schema'
import type { RunState } from '@/run/fold'

type RunDetail = components['schemas']['RunDetail']

/** Where a fallback price came from, in plain words. A reason that the run record names beats one that we infer. */
export type FallbackWhy = { reason: string; inferred: boolean; count: number }

export type RunHealth = {
  calls: number
  /** Model calls that ended with an error, by error kind. */
  failed: Record<string, number>
  failedTotal: number
  /** Model calls with attempt above 1. */
  retries: number
  /** Seller decisions in all, and how many of them were a fallback (the last price kept). */
  decisions: number
  fallbacks: number
  fallbackWhy: FallbackWhy[]
  /** Calls that the retry after a slow attempt sent with lower thinking (llm_call.degraded). */
  degraded: number
  /** Holds and PASS replies that the system recorded after two slow attempts. They fail the manipulation check (feature timeout_fallbacks). */
  timeoutFallbacks: number
  /** Rounds in which every seller fell back. */
  allFallbackRounds: number[]
  rounds: number
  /** Most prices are fallbacks: the metrics then describe the fallback rule, not the model. */
  mostFallback: boolean
}

const NO_CALL = 'no failed call is recorded for that turn'

/** The words for a fallback reason that the run record names (acted.fallback_reason, DECISIONS-22 entry 5.4). */
export const REASON_TEXT: Record<string, string> = {
  timeout: 'two slow attempts (timeout)',
  error: 'provider errors on every attempt',
  invalid: 'no valid tool call',
  max_steps: 'the steps ran out',
}

/** The words for an error kind of a model call (llm_call.error). */
export const ERROR_TEXT: Record<string, string> = {
  parse_error: 'the reply could not be read',
  invalid_tool: 'the model called a tool that does not exist or has bad arguments',
  timeout: 'the call timed out',
  rate_limit: 'the provider limited the rate',
  auth: 'the provider refused the key',
  not_found: 'the provider did not know the model',
  provider_error: 'the provider failed',
}

/**
 * What went wrong in a run, from the events up to now. The run record has no fallback reason today, so a fallback gets the error kind
 * of the last failed model call of the same seller in the same round, marked as inferred. When `acted` carries `fallback_reason`
 * (contract 0.35.0), that reason is used and is not marked.
 */
export function runHealth(state: RunState): RunHealth {
  const failed: Record<string, number> = {}
  let calls = 0
  let retries = 0
  let degraded = 0
  let timeoutFallbacks = 0
  let decisions = 0
  let fallbacks = 0
  const lastError = new Map<string, string>()
  const why = new Map<string, FallbackWhy>()
  const byRound = new Map<number, { all: number; fb: number }>()
  for (const e of state.events) {
    if (e.type === 'llm_call') {
      calls++
      if (e.attempt > 1) retries++
      if (e.degraded) degraded++
      if (e.error) {
        failed[e.error] = (failed[e.error] ?? 0) + 1
        lastError.set(`${e.t}:${e.agent_id}`, e.error)
      }
    } else if (e.type === 'acted') {
      decisions++
      const r = byRound.get(e.t) ?? { all: 0, fb: 0 }
      r.all++
      if (e.fallback) {
        fallbacks++
        r.fb++
        const named = e.fallback_reason
        if (named === 'timeout') timeoutFallbacks++
        const inferred = !named
        const guess = lastError.get(`${e.t}:${e.agent_id}`)
        const text = named ? (REASON_TEXT[named] ?? named) : guess ? (ERROR_TEXT[guess] ?? guess) : NO_CALL
        const k = `${inferred}:${text}`
        const row = why.get(k) ?? { reason: text, inferred, count: 0 }
        row.count++
        why.set(k, row)
      }
      byRound.set(e.t, r)
    } else if (e.type === 'talk_replied' && e.reason === 'timeout') {
      timeoutFallbacks++
    }
  }
  const failedTotal = Object.values(failed).reduce((a, b) => a + b, 0)
  return {
    calls,
    failed,
    failedTotal,
    retries,
    degraded,
    timeoutFallbacks,
    decisions,
    fallbacks,
    fallbackWhy: [...why.values()].sort((a, b) => b.count - a.count),
    allFallbackRounds: [...byRound].filter(([, r]) => r.all > 0 && r.fb === r.all).map(([t]) => t),
    rounds: byRound.size,
    mostFallback: decisions > 0 && fallbacks * 2 >= decisions,
  }
}

/** The failure of a run in one line each: the kind, the HTTP status, the provider's code and text, and where it happened (contract 0.38.0 `error_detail`). */
export function errorDetailLines(d: NonNullable<RunDetail['error_detail']>): { key: 'kind' | 'text' | 'where'; text: string }[] {
  const kind = [d.code, d.exception !== d.code ? `(${d.exception})` : null, d.http_status != null ? `HTTP ${d.http_status}` : null, d.provider_code ? `provider code ${d.provider_code}` : null].filter(Boolean).join(' ')
  const text = d.provider_message ?? d.message
  const where = d.where ? `During ${d.where.phase} of seller ${d.where.seller_id} in round ${d.where.t}.` : 'Outside a seller call.'
  return [{ key: 'kind', text: kind }, ...(text ? [{ key: 'text' as const, text: `${d.provider_message ? 'Provider message' : 'Message'}: ${text}` }] : []), { key: 'where', text: where }]
}

/** The failure detail of a run: the run record first, then the `run_ended` event of a live stream that has not reloaded the record. */
export function errorDetailOf(state: RunState, record?: RunDetail['error_detail']): RunDetail['error_detail'] {
  if (record) return record
  const ended = state.events.findLast((e) => e.type === 'run_ended')
  return ended && 'error_detail' in ended ? (ended.error_detail as RunDetail['error_detail']) : null
}

/** The end of a run in words, from the end reason and the status reason. */
export const END_TEXT: Record<string, string> = {
  horizon: 'the run reached its last round',
  stopped_by_user: 'a user stopped the run',
  budget_exceeded: 'a budget cap stopped the run',
  rate_window: 'the provider call window stopped the run',
  error: 'an error stopped the run',
}
