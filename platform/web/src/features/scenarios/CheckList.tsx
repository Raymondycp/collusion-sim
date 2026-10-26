import { Check, X } from 'lucide-react'

/** The scenario check, one line per check. A failed check names its event. */
export function CheckList({ checks, title = 'Check' }: { checks: { id?: string; ok?: boolean; message?: string; event_index?: number | null }[]; title?: string }) {
  const failed = checks.filter((c) => c.ok === false).length
  return (
    <div className="space-y-1">
      <p className="text-sm font-medium">
        {title}: {failed === 0 ? 'all checks pass' : `${failed} of ${checks.length} checks fail`}
      </p>
      <ul className="space-y-0.5 text-sm" aria-label={title}>
        {checks.map((c, i) => (
          <li key={`${c.id}-${i}`} className="flex gap-1.5">
            {c.ok ? <Check className="mt-0.5 size-4 shrink-0 text-emerald-600" aria-label="pass" /> : <X className="mt-0.5 size-4 shrink-0 text-red-600" aria-label="fail" />}
            <span>
              <span className="font-mono text-xs text-muted-foreground">{c.id}</span> {c.message}
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}
