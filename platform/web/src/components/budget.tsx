import type { components } from '@/api/schema'
import { cn } from '@/lib/utils'
import { short, usd } from '@/lib/format'

type S = components['schemas']
type Caps = { max_usd?: number | null; max_calls?: number | null; max_tokens?: number | null } | null | undefined
export type RateWindowRow = S['CostEstimate']['rate_window'][number]

/** "$0.5, 200 calls, 1,000,000 tokens": the caps that are set. Budgets need at least one (contract 0.9.0). */
export function capsText(b: Caps): string {
  const parts = [
    b?.max_usd != null && `$${b.max_usd}`,
    b?.max_calls != null && `${b.max_calls.toLocaleString()} calls`,
    b?.max_tokens != null && `${b.max_tokens.toLocaleString()} tokens`,
  ].filter(Boolean)
  return parts.length ? parts.join(', ') : 'no cap'
}

export type Cap = 'max_calls' | 'max_tokens' | 'max_usd'
/** One cap below a worst case: the value that clears it, its button label, and what the cap allows after the raise. */
export type Raise = { cap: Cap; value: number; label: string; spend: string }

const num = (n: number) => n.toLocaleString('en-US')
const usdUp = (usd: number) => Math.ceil(usd * 100) / 100

/**
 * The caps that are below a worst case, each with the value that clears it (FE-realbackend finding 2, CR-87 minor 2). `name` is
 * "max" for the caps of one run and "batch max" for the batch budget. `each` says what the worst case is for ("each run").
 */
export function raisesFor(caps: Caps, worst: { calls?: number | null; tokens?: number | null; usd?: number | null }, name: string, each: string): Raise[] {
  const out: Raise[] = []
  if (caps?.max_calls != null && worst.calls != null && caps.max_calls < worst.calls) out.push({ cap: 'max_calls', value: worst.calls, label: `${name} calls to ${num(worst.calls)}`, spend: `${each}: up to ${num(worst.calls)} calls` })
  if (caps?.max_tokens != null && worst.tokens != null && caps.max_tokens < worst.tokens) out.push({ cap: 'max_tokens', value: worst.tokens, label: `${name} tokens to ${num(worst.tokens)}`, spend: `${each}: up to ${num(worst.tokens)} tokens` })
  if (caps?.max_usd != null && worst.usd != null && caps.max_usd < worst.usd) out.push({ cap: 'max_usd', value: usdUp(worst.usd), label: `${name} USD to $${usdUp(worst.usd).toFixed(2)}`, spend: `${each}: up to $${usdUp(worst.usd).toFixed(2)}` })
  return out
}

/** One set cap against a worst case: "$4.81 of $5.00" with its share of the cap (UX review item 1). */
export type CapRow = { id: Cap; text: string; pct: number }

const pctText = (p: number) => (p >= 99 && p < 100 ? p.toFixed(1) : String(Math.round(p)))

/** The caps that are set, each with the worst case against it. The worst case is for one run, or for all runs against a batch budget. */
export function capRows(caps: Caps, worst: { calls?: number | null; tokens?: number | null; usd?: number | null }): CapRow[] {
  const rows: CapRow[] = []
  const add = (id: Cap, used: number | null | undefined, cap: number | null | undefined, text: (u: number, c: number) => string) => {
    if (cap == null || used == null) return
    // A cap of 0 is a valid "no calls" setting: any use is over it, and there is no share to print (CR-92 2).
    const pct = cap > 0 ? (used / cap) * 100 : used > 0 ? Infinity : 0
    rows.push({ id, text: `${text(used, cap)} (${cap > 0 ? `${pctText(pct)}%` : used > 0 ? 'over the cap' : '0%'})`, pct })
  }
  add('max_usd', worst.usd, caps?.max_usd, (u, c) => `${usd(u)} of ${usd(c)}`)
  add('max_calls', worst.calls, caps?.max_calls, (u, c) => `${num(u)} of ${num(c)} calls`)
  add('max_tokens', worst.tokens, caps?.max_tokens, (u, c) => `${short(u)} of ${short(c)} tokens`)
  return rows
}

/** One line: "Worst case against the caps: $4.81 of $5.00 (96%) · 1,800 of 2,000 calls (90%)". A share of 90% or more is amber, over 100% is red. */
export function CapUse({ rows, scope }: { rows: CapRow[]; scope: string }) {
  if (!rows.length) return null
  return (
    <p aria-label="Worst case against the caps" className="text-xs">
      <span className="text-muted-foreground">Worst case {scope} against the caps: </span>
      {rows.map((r, i) => (
        <span key={r.id} className={cn(r.pct > 100 ? 'font-medium text-red-600 dark:text-red-400' : r.pct >= 90 ? 'font-medium text-amber-700 dark:text-amber-400' : '')}>
          {i > 0 && <span className="text-muted-foreground"> · </span>}
          {r.text}
        </span>
      ))}
    </p>
  )
}

/** The caps by name for the sentence "The worst case is above the max calls and the max USD." */
export const overText = (raises: Raise[]) => raises.map((r) => r.label.replace(/ to .*/, '')).join(' and the ')

const tokens = (t: { input?: number; output?: number }) => ((t.input ?? 0) + (t.output ?? 0)).toLocaleString()

/** Worst-case totals, the 5-hour call windows and the server's warnings, shared by the run and experiment estimates. */
export function EstimateNotes({ calls, tokensMax, rateWindow, warnings }: { calls: number; tokensMax: { input?: number; output?: number }; rateWindow: RateWindowRow[]; warnings: string[] }) {
  return (
    <>
      <p>
        All runs together: at most {calls.toLocaleString()} calls and {tokens(tokensMax)} tokens.
      </p>
      {rateWindow.length > 0 && (
        <ul aria-label="5-hour call windows" className="text-xs">
          {rateWindow.map((r) => (
            <li key={r.profile_id} className={r.enough === false ? 'text-amber-700 dark:text-amber-400' : 'text-muted-foreground'}>
              {r.profile_id}: {r.room} of {r.max_calls_per_5h} calls left in its 5-hour window (this machine, calls in flight included), up to {r.calls_max} needed
            </li>
          ))}
        </ul>
      )}
      {warnings.length > 0 && (
        <ul aria-label="Estimate warnings" className="list-disc pl-4 text-amber-700 dark:text-amber-400">
          {warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      )}
    </>
  )
}

/** A profile with no price records no cost, so the cost is then a lower bound. Prices are optional (contract 0.9.0). */
export const NO_PRICE_NOTE = 'have no price, so their cost is not counted. A call or token cap stops them; max_usd alone cannot.'

/** Experiment spend: usd counts priced calls only, so it is a lower bound when some calls have no price. */
export function spendText(s: S['ExperimentSummary']['spend'], cap?: Caps): string {
  const money = `${s.usd_complete ? '' : 'at least '}${usd(s.usd)}`
  const of = (v: number | null | undefined, f: (n: number) => string) => (v != null ? ` of ${f(v)}` : '')
  return [
    `${s.calls.toLocaleString()}${of(cap?.max_calls, (n) => n.toLocaleString())} calls`,
    `${s.tokens.toLocaleString()}${of(cap?.max_tokens, (n) => n.toLocaleString())} tokens`,
    `${money}${of(cap?.max_usd, (n) => usd(n))}`,
    ...(s.unpriced ? [`${s.unpriced} calls with no price`] : []),
  ].join(', ')
}
