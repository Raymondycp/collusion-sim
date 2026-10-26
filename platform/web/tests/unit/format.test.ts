import { describe, expect, it } from 'vitest'
import { short, usd, usdCall } from '@/lib/format'
import { capRows } from '@/components/budget'

describe('money (UX review item 1)', () => {
  it('dollars have two decimals, a sub-cent amount keeps three significant digits, and an unknown amount is unknown', () => {
    expect([4.8062, 5, 0.131, 0.01].map(usd)).toEqual(['$4.81', '$5.00', '$0.13', '$0.01'])
    expect([0.00999, 0.0042, 0.000027, 0.0099949, 0.0000005].map(usd)).toEqual(['$0.00999', '$0.0042', '$0.000027', '$0.00999', '<$0.000001'])
    expect([0, -1].map(usd)).toEqual(['$0.00', '$0.00'])
    expect([null, undefined].map(usd)).toEqual(['unknown', 'unknown'])
  })

  it('the cost of one call keeps six decimals and the exponent form', () => {
    expect(usdCall(0.000027)).toBe('$0.000027 (2.7e-5)')
    expect(usdCall(0.0012)).toBe('$0.001200 (1.2e-3)')
    expect(usdCall(1.5)).toBe('$1.500000 (1.5e+0)')
    expect(usdCall(12.3)).toBe('$12.300000 (1.2e+1)')
    expect(usdCall(0)).toBe('$0.00')
    expect(usdCall(null)).toBe('unknown')
  })

  it('a long count is short', () => {
    expect([950, 12_345, 29_881_200].map(short)).toEqual(['950', '12.3k', '29.9M'])
  })
})

describe('the worst case against the caps', () => {
  const worst = { calls: 1800, tokens: 29_881_200, usd: 4.8062 }

  it('shows each set cap with its share, and none for a cap that is not set', () => {
    expect(capRows({ max_usd: 5, max_calls: 2000, max_tokens: 30_000_000 }, worst).map((r) => r.text)).toEqual(['$4.81 of $5.00 (96%)', '1,800 of 2,000 calls (90%)', '29.9M of 30.0M tokens (99.6%)'])
    expect(capRows({ max_calls: 2000 }, worst).map((r) => r.text)).toEqual(['1,800 of 2,000 calls (90%)'])
    expect(capRows(null, worst)).toEqual([])
  })

  it('a share over 100% is kept, and a missing worst case gives no row', () => {
    expect(capRows({ max_calls: 100 }, worst)[0]).toMatchObject({ text: '1,800 of 100 calls (1800%)', pct: 1800 })
    expect(capRows({ max_usd: 5 }, { calls: 1, tokens: 1, usd: null })).toEqual([])
  })

  it('a cap of 0 is over at any use, with no Infinity% (CR-92 2)', () => {
    const rows = capRows({ max_calls: 0 }, worst)
    expect(rows[0]!.text).toBe('1,800 of 0 calls (over the cap)')
    expect(rows[0]!.text).not.toMatch(/Infinity|NaN/)
    expect(capRows({ max_calls: 0 }, { calls: 0 })[0]!.text).toBe('0 of 0 calls (0%)')
    expect(rows[0]!.pct).toBe(Infinity)
    expect(capRows({ max_calls: 0 }, { calls: 0 })[0]!.pct).toBe(0)
  })
})
