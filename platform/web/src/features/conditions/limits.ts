import type { Capability, Condition } from '@/api/runConfig'

export const DEFAULT_QUERIES = 3

/**
 * The smaller of the run limit and the tool grant applies (RunLimits, DECISIONS-8 §2), so the card shows that one.
 * At 0 no read is offered (0.23.0: every research condition), so the card names the one that is 0.
 */
export function readCallsText(limits: Condition['limits'], cap: Capability): string {
  const limit = limits.max_queries_per_round ?? DEFAULT_QUERIES
  const grant = cap.max_queries_per_round ?? DEFAULT_QUERIES
  const n = Math.min(limit, grant)
  if (n === 0) return `No read calls (${limit === 0 ? 'limit 0' : 'tool grant 0'})`
  return limit === grant ? `Read calls per round: ${n}` : `Read calls per round: ${n} (limit ${limit}, tool grant ${grant})`
}
