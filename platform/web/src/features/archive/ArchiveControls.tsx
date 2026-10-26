import { useState } from 'react'
import { Archive, ArchiveRestore } from 'lucide-react'
import { asApiError } from '@/api/client'
import { useArchive, type ArchiveAction, type ArchiveKind, type ArchiveTarget } from '@/api/archive'
import { useExperiments } from '@/api/experiments'
import { RUNS_LIMIT, useRuns } from '@/api/queries'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'

const NOUN: Record<ArchiveKind, string> = { runs: 'run', experiments: 'experiment' }
const plural = (kind: ArchiveKind, n: number) => `${n} ${NOUN[kind]}${n === 1 ? '' : 's'}`

/**
 * Archive and Restore of a list on the server (DECISIONS-29). Archive changes the list for everyone and deletes nothing. It is not
 * Hide, which changes only this browser. `notice` says what happened, with the runs that `all` skipped; `error` is the server message.
 */
export function useArchiveActions(kind: ArchiveKind) {
  const m = useArchive(kind)
  const [notice, setNotice] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  /** True when the server accepted the call. A refusal (409 for a run that has not ended) leaves everything as it was. */
  const run = async (action: ArchiveAction, target: ArchiveTarget): Promise<boolean> => {
    setNotice(null)
    setError(null)
    try {
      const r = await m.mutateAsync({ action, target })
      const verb = action === 'archive' ? 'Archived' : 'Restored'
      const skipped = r.skipped.length ? ` Skipped ${plural(kind, r.skipped.length)} that ${r.skipped.length === 1 ? 'is' : 'are'} running: ${r.skipped.join(', ')}.` : ''
      setNotice(r.count === 0 && !skipped ? `Nothing changed: no ${NOUN[kind]} was in the other state.` : `${verb} ${plural(kind, r.count)}.${skipped}`)
      return true
    } catch (e) {
      setError(asApiError(e).message)
      return false
    }
  }
  return { run, busy: m.isPending, notice, error }
}

export function ArchiveStatus({ notice, error }: { notice: string | null; error: string | null }) {
  return (
    <>
      {error && (
        <p role="alert" className="rounded-md border border-red-500/40 bg-red-500/10 p-2 text-sm">
          {error}
        </p>
      )}
      {notice && (
        <p role="status" className="rounded-md border p-2 text-sm">
          {notice}
        </p>
      )}
    </>
  )
}

export function ShowArchived({ kind, checked, onChange }: { kind: ArchiveKind; checked: boolean; onChange: (on: boolean) => void }) {
  return (
    <label className="flex items-center gap-2 text-sm">
      <input type="checkbox" className="size-4 accent-current" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      Show archived {kind} (server)
    </label>
  )
}

type Scope = { ready: boolean; n: number; plus: boolean; live: number }

/** What the server acts on for `all`: every record that the server lists in the asked state, whatever the page filters show. */
function useRunsScope(action: ArchiveAction): Scope {
  const wantArchived = action === 'unarchive'
  const runs = useRuns(wantArchived ? { include_archived: true } : {})
  const items = (runs.data?.items ?? []).filter((r) => !!r.archived_at === wantArchived)
  return { ready: runs.isSuccess, n: items.length, plus: (runs.data?.items.length ?? 0) >= RUNS_LIMIT, live: wantArchived ? 0 : items.filter((r) => r.status === 'running').length }
}

function useExperimentsScope(action: ArchiveAction): Scope {
  const wantArchived = action === 'unarchive'
  const exps = useExperiments(wantArchived)
  const items = (exps.data ?? []).filter((x) => !!x.archived_at === wantArchived)
  return { ready: exps.isSuccess, n: items.length, plus: false, live: wantArchived ? 0 : items.filter((x) => x.status === 'running').length }
}

function AllDialog({ kind, action, scope, shown, onConfirm, onClose }: { kind: ArchiveKind; action: ArchiveAction; scope: Scope; shown: number; onConfirm: () => void; onClose: () => void }) {
  const count = scope.ready ? `${scope.n}${scope.plus ? '+' : ''}` : '…'
  const things = `${count} ${scope.ready && scope.n === 1 && !scope.plus ? NOUN[kind] : kind}`
  // The server leaves running records out, so the button names what it will archive: "Archive 16 of 18 runs (2 are running)" (CR-110 note 1).
  const running = action === 'archive' && scope.ready && scope.live > 0 && scope.live < scope.n
  const todo = running ? `${scope.n - scope.live} of ${things} (${scope.live} ${scope.live === 1 ? 'is' : 'are'} running)` : things
  const verb = action === 'archive' ? 'Archive' : 'Restore'
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {verb} all {things}
            {action === 'unarchive' ? ` that ${scope.n === 1 ? 'is' : 'are'} archived` : ''} on the server (not only the {shown} shown)?
          </DialogTitle>
          <DialogDescription>
            {action === 'archive'
              ? `This archives ${running ? `${scope.n - scope.live} of ${things}` : things} on the server, for everyone. Nothing is deleted: an archived ${NOUN[kind]} still opens by its id, and Restore brings it back. This is not Hide, which changes only this browser.`
              : `This restores ${things} that ${scope.n === 1 ? 'is' : 'are'} archived, on the server, for everyone.`}
          </DialogDescription>
        </DialogHeader>
        <ul className="list-disc space-y-1 pl-5 text-sm">
          {scope.plus && <li>The count is the newest {RUNS_LIMIT} that the list loads. The server may act on more.</li>}
          {scope.live > 0 && (
            <li>
              {plural(kind, scope.live)} {scope.live === 1 ? 'is' : 'are'} running. {scope.live === 1 ? 'It stays' : 'They stay'} in the list, and the answer names {scope.live === 1 ? 'it' : 'them'}.
            </li>
          )}
        </ul>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button disabled={!scope.ready || scope.n === 0 || scope.live === scope.n} onClick={onConfirm}>
            {verb} {todo}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

type DialogProps = { action: ArchiveAction; shown: number; onConfirm: () => void; onClose: () => void }

function RunsDialog(p: DialogProps) {
  return <AllDialog kind="runs" scope={useRunsScope(p.action)} {...p} />
}

function ExperimentsDialog(p: DialogProps) {
  return <AllDialog kind="experiments" scope={useExperimentsScope(p.action)} {...p} />
}

/** The "Archive all" button, or "Restore all" while the archived records show. It asks first and names the count. */
export function ArchiveAll({ kind, action, busy, shown, onRun }: { kind: ArchiveKind; action: ArchiveAction; busy: boolean; shown: number; onRun: (a: ArchiveAction, t: ArchiveTarget) => Promise<boolean> }) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <Button variant="outline" size="sm" disabled={busy} onClick={() => setOpen(true)}>
        {action === 'archive' ? <Archive /> : <ArchiveRestore />} {action === 'archive' ? 'Archive all' : 'Restore all'}
      </Button>
      {open &&
        (() => {
          const Confirm = kind === 'runs' ? RunsDialog : ExperimentsDialog
          return (
            <Confirm
              action={action}
              shown={shown}
              onClose={() => setOpen(false)}
              onConfirm={() => {
                setOpen(false)
                void onRun(action, { all: true })
              }}
            />
          )
        })()}
    </>
  )
}
