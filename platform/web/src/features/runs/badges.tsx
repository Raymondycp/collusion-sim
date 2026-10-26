import type { Labels, RunKind, RunStatus } from '@/api/types'
import { cn } from '@/lib/utils'

export function KindBadge({ kind }: { kind: RunKind }) {
  return (
    <span
      className={cn(
        'inline-flex items-center rounded px-1.5 py-0.5 text-xs font-medium',
        kind === 'research'
          ? 'bg-[var(--kind-research)]/12 text-[var(--kind-research)] ring-1 ring-[var(--kind-research)]/40'
          : 'border border-dashed border-[var(--kind-exploratory)] text-[var(--kind-exploratory)]',
      )}
    >
      {kind === 'research' ? 'Research' : 'Exploratory'}
    </span>
  )
}

const STATUS_STYLE: Record<RunStatus, string> = {
  queued: 'bg-muted text-muted-foreground',
  running: 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300',
  paused: 'bg-amber-500/15 text-amber-700 dark:text-amber-300',
  stopping: 'bg-amber-500/15 text-amber-700 dark:text-amber-300',
  recovering: 'bg-amber-500/15 text-amber-700 dark:text-amber-300',
  completed: 'bg-muted text-foreground',
  stopped: 'bg-muted text-muted-foreground',
  failed: 'bg-red-500/15 text-red-700 dark:text-red-300',
}

export function StatusBadge({ status }: { status: RunStatus }) {
  return (
    <span className={cn('inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-xs font-medium', STATUS_STYLE[status])}>
      {status === 'running' && <span aria-hidden className="size-1.5 rounded-full bg-current" />}
      {status}
    </span>
  )
}

/** Labels come from the condition file. Exploratory runs have none. */
/** A research run with no labels is an ablation branch: the contract gives it `labels: null`. */
export function LabelsText({ labels, kind }: { labels: Labels | null | undefined; kind: RunKind }) {
  if (!labels) return <span className="text-muted-foreground">{kind === 'research' ? 'None (ablation branch)' : 'None (exploratory)'}</span>
  return (
    <span title="From the condition file">
      legal <b>{labels.legal}</b> · harm <b>{labels.harm}</b>
    </span>
  )
}
