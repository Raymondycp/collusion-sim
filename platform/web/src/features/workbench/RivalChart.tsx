import { useState } from 'react'
import type { RunState } from '@/run/fold'
import { pricePairs } from './derive'

const W = 320
const PAD = 32

/**
 * Market chart 2 (UI-WORKBENCH.md §4): one dot per settled round up to the cursor, own price against the mean rival price.
 * The move from the Nash mark to the joint-optimum mark along the diagonal is the visual sign of coordination.
 */
export function RivalChart({ state, cursorT }: { state: RunState; cursorT: number }) {
  const [picked, setPicked] = useState<string | null>(null)
  const seller = picked && state.sellers.includes(picked) ? picked : (state.sellers[0] ?? null)
  const points = seller ? pricePairs(state, seller).filter((p) => p.t <= cursorT) : []
  const ref = [...state.reference].reverse().find((r) => r.t <= cursorT) ?? state.reference[0]
  const marks = [
    { name: 'Nash', v: ref?.nash ?? null },
    { name: 'Joint optimum', v: ref?.joint_optimum ?? null },
  ].flatMap((m) => (m.v === null ? [] : [{ name: m.name, v: m.v }]))

  const values = [...points.flatMap((p) => [p.own, p.rival]), ...marks.map((m) => m.v)]
  const lo = values.length ? Math.min(...values) : 0
  const hi = values.length ? Math.max(...values) : 1
  const span = hi - lo || 1
  const min = lo - span * 0.08
  const max = hi + span * 0.08
  const sx = (v: number) => PAD + ((v - min) / (max - min)) * (W - 2 * PAD)
  const sy = (v: number) => W - PAD - ((v - min) / (max - min)) * (W - 2 * PAD)
  const firstT = points[0]?.t ?? 0
  const lastT = points.at(-1)?.t ?? 1

  return (
    <section aria-label="Own price against rival price" className="rounded-lg border p-2">
      <div className="flex items-center justify-between gap-2 px-1 pb-1">
        <h3 className="text-sm font-medium">Own price against mean rival price</h3>
        {state.sellers.length > 2 && (
          <select aria-label="Seller on x" className="rounded border bg-background px-1 text-xs" value={seller ?? ''} onChange={(e) => setPicked(e.target.value)}>
            {state.sellers.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        )}
      </div>
      {points.length === 0 ? (
        <p className="p-3 text-xs text-muted-foreground">No settled round up to R{cursorT}.</p>
      ) : (
        <svg viewBox={`0 0 ${W} ${W}`} className="mx-auto block w-full max-w-sm" role="img" aria-label={`${points.length} rounds, seller ${seller} on x`}>
          <line x1={sx(min)} y1={sy(min)} x2={sx(max)} y2={sy(max)} stroke="var(--muted-foreground)" strokeDasharray="4 4" strokeWidth={1} />
          <line x1={PAD} y1={W - PAD} x2={W - PAD} y2={W - PAD} stroke="var(--border)" />
          <line x1={PAD} y1={PAD} x2={PAD} y2={W - PAD} stroke="var(--border)" />
          <text x={W / 2} y={W - 6} textAnchor="middle" fontSize={10} fill="var(--muted-foreground)">
            price of {seller}
          </text>
          <text x={10} y={W / 2} textAnchor="middle" fontSize={10} fill="var(--muted-foreground)" transform={`rotate(-90 10 ${W / 2})`}>
            mean rival price
          </text>
          {[min, max].map((v) => (
            <text key={v} x={sx(v)} y={W - PAD + 12} textAnchor="middle" fontSize={9} fill="var(--muted-foreground)">
              {v.toFixed(1)}
            </text>
          ))}
          {points.map((p) => {
            const k = lastT === firstT ? 1 : (p.t - firstT) / (lastT - firstT)
            return (
              <circle key={p.t} data-t={p.t} cx={sx(p.own)} cy={sy(p.rival)} r={p.t === cursorT ? 5 : 3.5} fill="var(--series-1)" fillOpacity={0.2 + 0.8 * k} stroke={p.t === cursorT ? 'var(--foreground)' : 'none'}>
                <title>{`R${p.t}: ${p.own}, rivals ${p.rival.toFixed(2)}`}</title>
              </circle>
            )
          })}
          {marks.map((m) => (
            <g key={m.name} aria-label={`${m.name} ${m.v}`}>
              <rect x={sx(m.v) - 4} y={sy(m.v) - 4} width={8} height={8} fill="none" stroke={m.name === 'Nash' ? 'var(--muted-foreground)' : 'var(--destructive)'} strokeWidth={1.5} />
              <text x={sx(m.v) + (m.v > (min + max) / 2 ? -7 : 7)} y={sy(m.v) + 3} textAnchor={m.v > (min + max) / 2 ? 'end' : 'start'} fontSize={9} fill="var(--muted-foreground)">
                {m.name}
              </text>
            </g>
          ))}
        </svg>
      )}
      <p className="px-1 text-xs text-muted-foreground">Dashed: equal prices. Squares: Nash (grey) and joint optimum (red) of R{ref?.t ?? '?'}. Light dots are early rounds, dark dots late rounds.</p>
    </section>
  )
}
