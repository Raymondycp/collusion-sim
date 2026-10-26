/**
 * Money, one way everywhere (UX review item 1): dollars with two decimals. Under one cent the amount keeps three significant
 * digits ("$0.0042"), so a budget of a few cents does not lose its resolution (CR-92 3, lead decision). An amount under a
 * millionth of a dollar reads "<$0.000001". An unknown amount reads "unknown".
 */
export function usd(v: number | null | undefined): string {
  if (v == null) return 'unknown'
  if (v <= 0) return '$0.00'
  if (v >= 0.01) return `$${v.toFixed(2)}`
  if (v < 1e-6) return '<$0.000001'
  return `$${v.toFixed(2 - Math.floor(Math.log10(v))).replace(/0+$/, '')}`
}

/** The cost of one model call: six decimals, with the exponent form beside it, for example "$0.000027 (2.7e-5)". */
export function usdCall(v: number | null | undefined): string {
  if (v == null) return 'unknown'
  return v === 0 ? '$0.00' : `$${v.toFixed(6)} (${v.toExponential(1)})`
}

/** A count in millions or thousands for a short line, for example 29,881,200 as "29.9M". */
export function short(n: number): string {
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`
  if (n >= 1e4) return `${(n / 1e3).toFixed(1)}k`
  return n.toLocaleString('en-US')
}
