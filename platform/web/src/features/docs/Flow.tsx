import { ChevronRight } from 'lucide-react'
import type { ReactNode } from 'react'
import { cn } from '@/lib/utils'

const REPO = 'https://github.com/Raymondycp/apart-hackathon/blob/sprint/'

/** Link to a file in the repo on GitHub (branch sprint). */
export function RepoLink({ path, children }: { path: string; children?: ReactNode }) {
  return (
    <a className="underline underline-offset-4" href={REPO + path} target="_blank" rel="noreferrer noopener">
      {children ?? <code className="text-xs">{path}</code>}
    </a>
  )
}

export function Code({ children }: { children: ReactNode }) {
  return <code className="text-xs">{children}</code>
}

export type FlowStep = { title: string; note?: string; gate?: boolean }

/** A row of boxes joined by arrows. Plain HTML, so it follows the light and dark theme. A gate step has a dashed border. */
export function Flow({ steps, label }: { steps: FlowStep[]; label: string }) {
  return (
    <ol aria-label={label} className="flex flex-wrap items-stretch gap-y-2 text-sm">
      {steps.map((s, i) => (
        <li key={s.title} className="flex items-center">
          <div className={cn('h-full rounded-md border bg-card px-3 py-2', s.gate && 'border-dashed bg-muted')}>
            <div className="font-medium">{s.title}</div>
            {s.note && <div className="text-xs text-muted-foreground">{s.note}</div>}
          </div>
          {i < steps.length - 1 && <ChevronRight aria-hidden className="mx-1 size-4 shrink-0 text-muted-foreground" />}
        </li>
      ))}
    </ol>
  )
}

export function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="space-y-2">
      <h2 className="text-lg font-semibold">{title}</h2>
      {children}
    </section>
  )
}

export function DataTable({ head, rows }: { head: string[]; rows: ReactNode[][] }) {
  return (
    <div className="overflow-x-auto rounded-lg border">
      <table className="w-full text-left text-sm">
        <thead className="bg-muted text-xs text-muted-foreground">
          <tr>
            {head.map((h) => (
              <th key={h} className="px-3 py-2 font-medium">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y align-top">
          {rows.map((r, i) => (
            <tr key={i}>
              {r.map((c, j) => (
                <td key={j} className="px-3 py-2">
                  {c}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
