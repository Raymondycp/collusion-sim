import { useEffect, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Pause, Play, Square } from 'lucide-react'
import { api, unwrap } from '@/api/client'
import type { RunStatus } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'

type Action = 'pause' | 'resume' | 'stop'

/** What a person can do in each status. The backend acts at the next round boundary. */
const ACTIONS: Record<RunStatus, Action[]> = {
  queued: ['stop'],
  running: ['pause', 'stop'],
  paused: ['resume', 'stop'],
  stopping: [],
  recovering: [],
  completed: [],
  stopped: [],
  failed: [],
}

const LABEL: Record<Action, string> = { pause: 'Pause', resume: 'Resume', stop: 'Stop' }
const ICON = { pause: Pause, resume: Play, stop: Square }

/** What a stop means for a research run (UI-WORKBENCH.md §3, CR-49). Pilot and exploratory runs need no reason. */
const STOP_TEXT: Record<string, string> = {
  test: 'This test cell stays locked: it cannot be run again (only as an exploratory run). The stopped run counts in the analysis.',
  dev: 'This stopped dev run stays in the record and counts in the analysis.',
}
// The run record has not loaded: it can be a dev or test run, so the stop asks for a reason (CR-54 2).
const UNKNOWN_SPLIT = 'The split of this run is not known yet. If it is a dev or test run, the stopped run counts in the analysis.'

function control(runId: string, action: Action, reason?: string) {
  const params = { params: { path: { run_id: runId } } }
  if (action === 'pause') return unwrap(api.POST('/runs/{run_id}/pause', params))
  if (action === 'resume') return unwrap(api.POST('/runs/{run_id}/resume', params))
  return unwrap(api.POST('/runs/{run_id}/stop', { ...params, body: reason ? { reason } : {} }))
}

/**
 * Pause, resume and stop over REST: the socket is read-only (FE-PLAN). The response can show the status from
 * before the action took effect, so the buttons follow `status` from the stream, not the response.
 */
export function RunControls({ runId, status, split }: { runId: string; status: RunStatus | null | undefined; split: string | null | undefined }) {
  const qc = useQueryClient()
  const [confirmStop, setConfirmStop] = useState(false)
  const [reason, setReason] = useState('')
  const [sentReason, setSentReason] = useState<string | null>(null)
  const research = split === undefined ? UNKNOWN_SPLIT : split ? STOP_TEXT[split] : undefined
  const act = useMutation({
    mutationFn: (action: Action) => control(runId, action, action === 'stop' && research ? reason.trim() : undefined),
    onSuccess: (_, action) => action === 'stop' && research && setSentReason(reason.trim()),
    onSettled: () => qc.invalidateQueries({ queryKey: ['run', runId] }),
  })
  const { reset } = act
  // An error belongs to the status it was made in: once the stream shows a new status, it no longer applies.
  useEffect(() => reset(), [status, reset])
  const actions = status ? ACTIONS[status] : []
  // The server takes the reason (contract) but does not store it yet, so the page shows it for this visit.
  const note = sentReason && <p className="basis-full text-xs text-muted-foreground">Stop reason (not stored by the server): {sentReason}</p>
  if (actions.length === 0) return note || null

  const run = (a: Action) => (a === 'stop' ? setConfirmStop(true) : act.mutate(a))
  return (
    <>
      {note}
      {actions.map((a) => {
        const Icon = ICON[a]
        return (
          <Button key={a} variant="outline" size="sm" disabled={act.isPending} onClick={() => run(a)}>
            <Icon /> {LABEL[a]}
          </Button>
        )
      })}
      {act.error && (
        <p role="alert" className="basis-full text-sm text-red-600 dark:text-red-400">
          {act.error.message}
        </p>
      )}
      <Dialog open={confirmStop} onOpenChange={setConfirmStop}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Stop {runId}?</DialogTitle>
            <DialogDescription>The run stops after the current round and cannot continue. It keeps its record and its ledger line.</DialogDescription>
          </DialogHeader>
          {research && (
            <div className="space-y-2 text-sm">
              <p className="font-medium">{research}</p>
              <label className="block space-y-1">
                <span>Reason for the stop (required)</span>
                <textarea
                  className="w-full rounded-md border bg-background p-2"
                  rows={3}
                  maxLength={500}
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                />
              </label>
              <p className="text-xs text-muted-foreground">The reason goes with the stop request. This server does not store it yet: write it in the run notes too.</p>
            </div>
          )}
          <DialogFooter>
            <Button variant="ghost" onClick={() => setConfirmStop(false)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={act.isPending || (!!research && !reason.trim())}
              onClick={() => {
                setConfirmStop(false)
                act.mutate('stop')
              }}
            >
              Stop run
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
