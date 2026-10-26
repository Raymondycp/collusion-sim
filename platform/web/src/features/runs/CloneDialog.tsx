import { useMemo, useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router'
import { runPath } from '@/lib/paths'
import { useQueryClient } from '@tanstack/react-query'
import { Plus, X } from 'lucide-react'
import { ApiError } from '@/api/client'
import { cloneRun, useRun, type CloneRequest } from '@/api/queries'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { NativeSelect } from '@/features/new-run/SchemaField'
import { overridable, toOverrides, toPointer, type OverrideRow } from './clone'

const show = (v: unknown) => (typeof v === 'string' ? v : JSON.stringify(v))

export function CloneDialog({ runId, onClose }: { runId: string; onClose: () => void }) {
  const run = useRun(runId)
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [sameSeed, setSameSeed] = useState(true)
  const [seed, setSeed] = useState('')
  const [name, setName] = useState('')
  const [rows, setRows] = useState<OverrideRow[]>([])
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [serverError, setServerError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const d = run.data
  const research = d?.kind === 'research'
  const fields = useMemo(() => new Map(d ? overridable(d.config) : []), [d])

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (!d) return
    const next: Record<string, string> = {}
    const n = Number(seed)
    if (!sameSeed && !(seed.trim() !== '' && Number.isInteger(n) && n >= 0)) next.seed = 'Enter a whole number, 0 or more.'
    const o = toOverrides(rows, fields)
    if ('errors' in o) for (const [i, msg] of Object.entries(o.errors)) next[`row${i}`] = msg
    setErrors(next)
    setServerError(null)
    if (Object.keys(next).length || 'errors' in o) return
    const body: CloneRequest = { seed: sameSeed ? null : n, ...(name.trim() ? { name: name.trim() } : {}), ...(rows.length ? { overrides: o.overrides } : {}) }
    setBusy(true)
    try {
      const res = await cloneRun(runId, body)
      await qc.invalidateQueries({ queryKey: ['runs'] })
      // Close first: on the run page the same page stays mounted for the new run id.
      onClose()
      navigate(runPath(res.run_ids[0]!))
    } catch (err) {
      if (!(err instanceof ApiError)) throw err
      const byField: Record<string, string> = {}
      const rest: string[] = []
      for (const f of err.fields) {
        const i = rows.findIndex((r) => f.path === `overrides.${toPointer(r.path)}`)
        if (i >= 0) byField[`row${i}`] = f.message
        else if (f.path === 'seed') byField.seed = f.message
        else rest.push(f.message)
      }
      setErrors(byField)
      setServerError(rest.length || !Object.keys(byField).length ? [err.message, ...rest.filter((m) => m !== err.message)].join(' ') : 'The backend refused the clone. Correct the marked field.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Clone and re-run</DialogTitle>
          <DialogDescription>
            Starts a new run from the config of {runId}. The ledger links the new run to this one. This run does not change.
          </DialogDescription>
        </DialogHeader>
        {!d ? (
          <p className="text-sm text-muted-foreground">{run.error?.message ?? 'Loading the run…'}</p>
        ) : (
          <form onSubmit={submit} className="space-y-4" noValidate>
            <fieldset className="space-y-2">
              <legend className="text-sm font-medium">Seed</legend>
              <label className="flex items-center gap-2 text-sm">
                <input type="radio" name="clone-seed" className="size-4" checked={sameSeed} onChange={() => setSameSeed(true)} />
                The same seed ({d.seed}): the same config hash, a new run id
              </label>
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <label className="flex items-center gap-2">
                  <input type="radio" name="clone-seed" className="size-4" checked={!sameSeed} onChange={() => setSameSeed(false)} />
                  Another seed
                </label>
                <Input
                  id="clone-seed"
                  aria-label="Seed"
                  inputMode="numeric"
                  className="h-8 w-28"
                  value={seed}
                  onFocus={() => setSameSeed(false)}
                  onChange={(e) => setSeed(e.target.value)}
                  aria-invalid={!!errors.seed || undefined}
                  aria-describedby={errors.seed ? 'clone-seed-error' : undefined}
                />
              </div>
              {errors.seed && (
                <p id="clone-seed-error" className="text-xs text-red-600 dark:text-red-400">
                  {errors.seed}
                </p>
              )}
              {research && <p className="text-xs text-muted-foreground">A research seed must be in the {d.split} split. The backend refuses a test seed after the freeze.</p>}
            </fieldset>

            <div className="space-y-1">
              <label htmlFor="clone-name" className="block text-sm font-medium">
                Name <span className="font-normal text-muted-foreground">(optional)</span>
              </label>
              <Input id="clone-name" value={name} maxLength={120} autoComplete="off" onChange={(e) => setName(e.target.value)} />
            </div>

            {research ? (
              <p className="rounded-md border border-dashed p-2 text-sm text-muted-foreground">
                This is a research run. Condition {d.condition_id} sets its channels, capabilities and prompts, so a clone can change only the seed.
              </p>
            ) : (
              <fieldset className="space-y-2">
                <legend className="text-sm font-medium">Changes</legend>
                {rows.length === 0 && <p className="text-xs text-muted-foreground">No changes: the clone uses the same config.</p>}
                {rows.map((r, i) => (
                  <div key={i} className="space-y-1 rounded-md border p-2">
                    <div className="flex items-start gap-2">
                      <div className="min-w-0 flex-1 space-y-1">
                        <NativeSelect
                          id={`clone-field-${i}`}
                          aria-label={`Field ${i + 1}`}
                          value={r.path}
                          className="font-mono text-xs"
                          onChange={(e) => setRows((xs) => xs.map((x, j) => (j === i ? { path: e.target.value, text: show(fields.get(e.target.value)) } : x)))}
                        >
                          <option value="">Select a field…</option>
                          {[...fields.keys()].map((k) => (
                            <option key={k} value={k}>
                              {k}
                            </option>
                          ))}
                        </NativeSelect>
                        {r.path && (
                          <p className="text-xs text-muted-foreground">
                            Now: <span className="font-mono break-all">{show(fields.get(r.path))}</span>
                          </p>
                        )}
                        <Input
                          id={`clone-value-${i}`}
                          aria-label={`New value ${i + 1}`}
                          className="font-mono text-xs"
                          value={r.text}
                          disabled={!r.path}
                          onChange={(e) => setRows((xs) => xs.map((x, j) => (j === i ? { ...x, text: e.target.value } : x)))}
                          aria-invalid={!!errors[`row${i}`] || undefined}
                          aria-describedby={errors[`row${i}`] ? `clone-row${i}-error` : undefined}
                        />
                      </div>
                      <Button type="button" size="icon" variant="ghost" className="size-8" aria-label={`Remove change ${i + 1}`} onClick={() => setRows((xs) => xs.filter((_, j) => j !== i))}>
                        <X />
                      </Button>
                    </div>
                    {errors[`row${i}`] && (
                      <p id={`clone-row${i}-error`} className="text-xs text-red-600 dark:text-red-400">
                        {errors[`row${i}`]}
                      </p>
                    )}
                  </div>
                ))}
                <Button type="button" variant="outline" size="sm" onClick={() => setRows((xs) => [...xs, { path: '', text: '' }])}>
                  <Plus /> Add a change
                </Button>
              </fieldset>
            )}

            {serverError && (
              <p role="alert" className="rounded-md border border-red-500/40 bg-red-500/10 p-2 text-sm">
                {serverError}
              </p>
            )}

            <DialogFooter className="items-center gap-2 sm:justify-between">
              <p className="text-xs text-muted-foreground">LLM sellers make paid calls.</p>
              <div className="flex gap-2">
                <Button type="button" variant="ghost" onClick={onClose}>
                  Cancel
                </Button>
                <Button type="submit" disabled={busy}>
                  {busy ? 'Starting…' : 'Start clone'}
                </Button>
              </div>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  )
}
