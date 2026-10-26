import { useState, type FormEvent } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Snowflake } from 'lucide-react'
import { FREEZE_KEY, postFreeze, stageOf, useFreezePreview, type FreezePreview, type StageId, type StageStatus } from '@/api/settings'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { ErrorText, PageHead } from './SettingsLayout'

/** The word a person types to confirm. A typed word, not a checkbox: the freeze decides which seeds open and which rule the test seeds judge. */
export const CONFIRM_WORD = 'freeze'

/** DECISIONS-11 §2.5: thresholds are tuned on dev, so the rule is frozen after dev, in its own stage. */
const STAGES: { id: StageId; short: string; title: string; opens: string; holds: string }[] = [
  { id: 'conditions', short: 'stage 1', title: 'Stage 1: conditions', opens: 'dev seeds', holds: 'condition files, seeds.yaml, seed scenarios, the feature code and the judge rubric' },
  { id: 'rule', short: 'stage 2', title: 'Stage 2: rule', opens: 'test seeds', holds: 'decision rule' },
]

function HashList({ label, hashes }: { label: string; hashes: Record<string, string | null> }) {
  return (
    <table aria-label={label} className="w-full text-sm">
      <thead>
        <tr className="text-left text-xs text-muted-foreground">
          <th className="py-1 pr-3 font-normal">Input</th>
          <th className="py-1 font-normal">Hash</th>
        </tr>
      </thead>
      <tbody>
        {Object.entries(hashes).map(([name, hash]) => (
          <tr key={name} className="border-t align-top">
            <td className="py-1 pr-3 font-mono text-xs break-all">{name}</td>
            <td className={hash ? 'py-1 font-mono text-xs break-all' : 'py-1 text-xs text-red-700 dark:text-red-400'}>{hash || 'missing'}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function stageText(s: StageStatus, opens: string): string {
  if (!s.frozen) return `Not frozen. The ${opens} stay locked.`
  const when = `Frozen at ${s.frozen_at ? new Date(s.frozen_at).toLocaleString() : 'an unknown time'} by ${s.approved_by ?? 'an unknown person'}`
  return s.valid ? `${when}. The ${opens} are open.` : `${when}, but it is not valid now. The ${opens} are locked again.`
}

/** One stage: its status, its inputs, and its own approver and typed confirmation (CR-27 M1). */
function StageSection({ st, preview, blockedBy }: { st: (typeof STAGES)[number]; preview: FreezePreview; blockedBy: string | null }) {
  const qc = useQueryClient()
  const [approver, setApprover] = useState('')
  const [word, setWord] = useState('')
  const freeze = useMutation({
    mutationFn: () => postFreeze(st.id, approver.trim()),
    onSuccess: async () => {
      setWord('')
      await Promise.all([qc.invalidateQueries({ queryKey: FREEZE_KEY }), qc.invalidateQueries({ queryKey: ['conditions'] }), qc.invalidateQueries({ queryKey: ['seeds'] })])
    },
  })
  const inputs = preview.inputs && Object.fromEntries(Object.entries(preview.inputs).filter(([k]) => stageOf(k) === st.id))
  const missing = Object.entries(inputs ?? {}).filter(([, h]) => !h).map(([k]) => k)
  const done = preview.stages[st.id]
  const ready = approver.trim() !== '' && word === CONFIRM_WORD && missing.length === 0 && !blockedBy && !freeze.isPending
  const id = `fz-${st.id}`

  function submit(e: FormEvent) {
    e.preventDefault()
    if (ready) freeze.mutate()
  }

  return (
    <section aria-label={st.title} className="space-y-2 rounded-lg border p-3">
      <h2 className="font-medium">{st.title}</h2>
      <p className="text-xs text-muted-foreground">Holds the {st.holds}. Opens the {st.opens}.</p>
      <p role="status" className="text-sm">{stageText(done, st.opens)}</p>
      {done.frozen && !done.valid && (
        <ul aria-label={`${st.title} problems`} className="ml-4 list-disc text-sm text-red-700 dark:text-red-400">
          {done.problems.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
      )}
      {inputs && <HashList label={`${st.title} inputs`} hashes={inputs} />}
      {freeze.data && (
        <div className="space-y-2 rounded-md border border-emerald-500/40 p-2">
          <h3 className="text-sm font-medium">Stored in conditions/frozen.json</h3>
          <HashList label="Frozen hashes" hashes={freeze.data.hashes} />
          <p className="text-sm text-muted-foreground">Next: the lead commits the changed files and frozen.json.</p>
        </div>
      )}
      <form onSubmit={submit} className="space-y-3 border-t pt-3">
        {blockedBy && <p className="text-sm text-red-700 dark:text-red-400">{blockedBy}</p>}
        {missing.length > 0 && <p className="text-sm text-red-700 dark:text-red-400">Missing: {missing.join(', ')}. Add them first (Research files).</p>}
        <div className="space-y-1">
          <Label htmlFor={`${id}-approver`}>Approved by</Label>
          <Input id={`${id}-approver`} autoComplete="off" maxLength={80} value={approver} onChange={(e) => setApprover(e.target.value)} />
        </div>
        <div className="space-y-1">
          <Label htmlFor={`${id}-confirm`}>Type {CONFIRM_WORD} to confirm</Label>
          <Input id={`${id}-confirm`} autoComplete="off" spellCheck={false} value={word} onChange={(e) => setWord(e.target.value)} />
        </div>
        <ErrorText>{freeze.error?.message}</ErrorText>
        <Button type="submit" disabled={!ready}>
          <Snowflake /> {freeze.isPending ? 'Freezing…' : done.frozen ? `Freeze ${st.short} again` : `Freeze ${st.short}`}
        </Button>
      </form>
    </section>
  )
}

export function FreezePage() {
  const status = useFreezePreview()
  const s = status.data
  const conditionsOk = s?.stages.conditions.valid === true
  return (
    <div className="max-w-3xl space-y-4">
      <PageHead title="Freeze">
        Two stages. Stage 1 freezes the conditions and opens the dev seeds. Stage 1 also freezes the judge rubric. Stage 2 freezes the decision rule after the dev work, and opens the test seeds. A new stage 1 removes stage 2. The server refuses a freeze once a test cell is used. The server writes conditions/frozen.json. It never commits; the lead commits the files.
      </PageHead>
      <ErrorText>{status.error?.message}</ErrorText>
      {status.isPending && <Skeleton className="h-40 w-full" />}
      {s && !s.inputs && <p className="text-sm text-muted-foreground">This backend does not list the freeze inputs yet, so the hashes show only after the freeze.</p>}
      {s && !!s.problems?.length && (
        <ul aria-label="Freeze problems" className="ml-4 list-disc text-sm text-muted-foreground">
          {s.problems.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
      )}
      {s &&
        STAGES.map((st) => (
          <StageSection key={st.id} st={st} preview={s} blockedBy={st.id === 'rule' && !conditionsOk ? 'Freeze stage 1 first. Stage 2 needs stage 1 with the condition hashes of now.' : null} />
        ))}
    </div>
  )
}
