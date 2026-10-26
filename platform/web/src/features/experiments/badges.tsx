import type { CellStatus, ExperimentSummary } from '@/api/experiments'
import { cn } from '@/lib/utils'

type Status = CellStatus | ExperimentSummary['status']

const STYLE: Record<Status, string> = {
  planned: 'border border-dashed text-muted-foreground',
  queued: 'bg-muted text-muted-foreground',
  running: 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300',
  branching: 'bg-sky-500/15 text-sky-700 dark:text-sky-300',
  paused: 'bg-amber-500/15 text-amber-700 dark:text-amber-300',
  stopping: 'bg-amber-500/15 text-amber-700 dark:text-amber-300',
  recovering: 'bg-amber-500/15 text-amber-700 dark:text-amber-300',
  blocked: 'bg-red-500/15 text-red-700 ring-1 ring-red-500/50 dark:text-red-300',
  completed: 'bg-muted text-foreground',
  ended: 'bg-muted text-muted-foreground',
  stopped: 'bg-muted text-muted-foreground',
  failed: 'bg-red-500/15 text-red-700 dark:text-red-300',
}

export function ExperimentStatusBadge({ status }: { status: Status }) {
  return (
    <span className={cn('inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-xs font-medium', STYLE[status])}>
      {(status === 'running' || status === 'branching') && <span aria-hidden className="size-1.5 rounded-full bg-current" />}
      {status}
    </span>
  )
}

