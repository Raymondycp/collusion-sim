import { useState, type FormEvent } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2 } from 'lucide-react'
import { useProfiles } from '@/api/providers'
import { useDefaults, type Defaults } from '@/api/runConfig'
import { saveDefaults } from '@/api/settings'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { NativeSelect } from '@/features/new-run/SchemaField'
import { ErrorText, PageHead } from './SettingsLayout'

const ROLES = [
  { id: 'seller', label: 'Seller', about: 'Pre-fills each new exploratory LLM seller.' },
  { id: 'judge', label: 'Judge', about: 'The first judge backend.' },
  { id: 'judge2', label: 'Second judge', about: 'The judge from a different model family.' },
  { id: 'host_llm', label: 'Host (LLM)', about: 'An LLM host, when a run has one.' },
] as const

type Choice = { profile_id: string | null; model: string | null }
type Override = Choice & { id: string }
const AGENT_ID = /^[A-Z]$/

function PickFields({ prefix, label, value, onChange }: { prefix: string; label: string; value: Choice; onChange: (v: Choice) => void }) {
  const profiles = useProfiles()
  const missing = value.profile_id !== null && profiles.data && !profiles.data.some((p) => p.id === value.profile_id)
  return (
    <div className="grid gap-2 sm:grid-cols-2">
      <div className="space-y-1">
        <Label htmlFor={`${prefix}-profile`}>{label} profile</Label>
        <NativeSelect id={`${prefix}-profile`} value={value.profile_id ?? ''} onChange={(e) => onChange({ ...value, profile_id: e.target.value || null })}>
          <option value="">None</option>
          {missing && <option value={value.profile_id!}>{value.profile_id} (not found)</option>}
          {profiles.data?.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </NativeSelect>
      </div>
      <div className="space-y-1">
        <Label htmlFor={`${prefix}-model`}>{label} model</Label>
        <Input id={`${prefix}-model`} autoComplete="off" value={value.model ?? ''} placeholder="the profile's default model" onChange={(e) => onChange({ ...value, model: e.target.value || null })} />
      </div>
    </div>
  )
}

function DefaultsForm({ initial }: { initial: Defaults }) {
  const qc = useQueryClient()
  const [roles, setRoles] = useState<Record<string, Choice>>(() => Object.fromEntries(ROLES.map((r) => [r.id, { profile_id: initial[r.id]?.profile_id ?? null, model: initial[r.id]?.model ?? null }])))
  const [overrides, setOverrides] = useState<Override[]>(() =>
    Object.entries(initial.seller?.agents ?? {}).map(([id, v]) => ({ id, profile_id: v.profile_id ?? null, model: v.model ?? null })),
  )
  const [error, setError] = useState<string | null>(null)
  const save = useMutation({
    mutationFn: saveDefaults,
    onSuccess: (d) => qc.setQueryData(['defaults'], d),
  })

  function submit(e: FormEvent) {
    e.preventDefault()
    const ids = overrides.map((o) => o.id)
    if (ids.some((id) => !AGENT_ID.test(id))) return setError('A seller id is one capital letter, A to Z, as the run form gives. The server reads no other ids.')
    if (new Set(ids).size !== ids.length) return setError('Each agent id can have one override only.')
    setError(null)
    const body = Object.fromEntries(
      ROLES.map((r) => [
        r.id,
        { ...roles[r.id]!, agents: r.id === 'seller' ? Object.fromEntries(overrides.map(({ id, ...v }) => [id, v])) : (initial[r.id]?.agents ?? {}) },
      ]),
    ) as Defaults
    save.mutate(body)
  }

  return (
    <form onSubmit={submit} className="space-y-4">
      {ROLES.map((r) => (
        <fieldset key={r.id} className="space-y-2 rounded-lg border p-3">
          <legend className="px-1 text-sm font-medium">{r.label}</legend>
          <p className="text-xs text-muted-foreground">{r.about}</p>
          <PickFields prefix={`d-${r.id}`} label={r.label} value={roles[r.id]!} onChange={(v) => setRoles({ ...roles, [r.id]: v })} />
          {r.id === 'seller' && (
            <div className="space-y-2 pt-1">
              <p className="text-sm font-medium">Per seller</p>
              <ul aria-label="Seller overrides" className="space-y-2">
                {overrides.map((o, i) => (
                  <li key={i} className="space-y-2 rounded-md border p-2">
                    <div className="flex items-end gap-2">
                      <div className="space-y-1">
                        <Label htmlFor={`d-over-${i}-id`}>Agent id</Label>
                        <Input id={`d-over-${i}-id`} className="w-24 font-mono" value={o.id} onChange={(e) => setOverrides(overrides.map((x, j) => (j === i ? { ...x, id: e.target.value } : x)))} />
                      </div>
                      <Button type="button" size="icon" variant="ghost" className="size-8" aria-label={`Remove override ${o.id || i + 1}`} onClick={() => setOverrides(overrides.filter((_, j) => j !== i))}>
                        <Trash2 />
                      </Button>
                    </div>
                    <PickFields prefix={`d-over-${i}`} label={`Seller ${o.id || i + 1}`} value={o} onChange={(v) => setOverrides(overrides.map((x, j) => (j === i ? { ...x, ...v } : x)))} />
                  </li>
                ))}
              </ul>
              <Button type="button" size="sm" variant="outline" onClick={() => setOverrides([...overrides, { id: '', profile_id: null, model: null }])}>
                <Plus /> Add seller override
              </Button>
            </div>
          )}
        </fieldset>
      ))}
      <ErrorText>{error ?? save.error?.message}</ErrorText>
      <div className="flex items-center gap-3">
        <Button type="submit" disabled={save.isPending}>
          {save.isPending ? 'Saving…' : 'Save defaults'}
        </Button>
        {save.isSuccess && !save.isPending && <span role="status" className="text-sm text-muted-foreground">Saved.</span>}
      </div>
    </form>
  )
}

export function DefaultsPage() {
  const defaults = useDefaults()
  return (
    <div className="max-w-3xl space-y-4">
      <PageHead title="Defaults">
        The profile and model that pre-fill new exploratory agents. Values saved here replace the .env values (DECISIONS-11). A research run never reads them.
      </PageHead>
      <ErrorText>{defaults.error?.message}</ErrorText>
      {defaults.isPending && <Skeleton className="h-64 w-full" />}
      {defaults.data && <DefaultsForm initial={defaults.data} />}
    </div>
  )
}
