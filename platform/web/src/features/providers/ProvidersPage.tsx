import { useState, type FormEvent } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { KeyRound, Pencil, Plug, Plus, ShieldAlert, Trash2 } from 'lucide-react'
import {
  createProfile,
  deleteProfile,
  PROFILES_KEY,
  testProfile,
  updateProfile,
  useProfileModels,
  useProfiles,
  type ProfileTestResult,
  type ProviderProfile,
  type ProviderProfileCreate,
} from '@/api/providers'
import { ApiError } from '@/api/client'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { NativeSelect } from '@/features/new-run/SchemaField'

function ErrorText({ children }: { children: string | null }) {
  if (!children) return null
  return (
    <p role="alert" className="rounded-md border border-red-500/40 bg-red-500/10 p-2 text-sm">
      {children}
    </p>
  )
}

const isMock = () => import.meta.env.MODE === 'mock'

/** Until the backend has a key store, this page must not invite a real key. */
function NoKeyStore({ reason }: { reason: 'mock' | 'missing' }) {
  return (
    <div role="note" aria-label="No key store" className="flex gap-2 rounded-md border border-amber-500/50 bg-amber-500/10 p-3 text-sm">
      <ShieldAlert className="mt-0.5 size-4 shrink-0 text-amber-600 dark:text-amber-400" aria-hidden />
      <div className="space-y-1">
        <p className="font-medium">{reason === 'mock' ? 'Mock backend. Do not type a real API key here.' : 'This backend has no key store yet. Do not add a profile.'}</p>
        <p className="text-muted-foreground">
          {reason === 'mock'
            ? 'This page runs on the mock server in your browser. No key reaches a backend or the OS keyring. Use fake text, for example sk-test-0000.'
            : 'The provider-profile endpoints come with backend unit 7b. Until then, no key store exists to keep a key.'}
        </p>
      </div>
    </div>
  )
}

/** Reads the form once. Returns the request body, or the first problem with it. */
type Protocol = NonNullable<ProviderProfileCreate['protocol']>
/** The wire formats of contract 0.9.0. typesafe_systemone is registered but not built (422 protocol_not_built). */
const PROTOCOLS: { id: Protocol; label: string; built: boolean }[] = [
  { id: 'openai_chat', label: 'OpenAI Chat Completions', built: true },
  { id: 'openai_responses', label: 'OpenAI Responses', built: true },
  { id: 'anthropic_messages', label: 'Anthropic Messages', built: true },
  { id: 'google', label: 'Google', built: true },
  { id: 'typesafe_systemone', label: 'TypeSafe System One (not built yet)', built: false },
]
const protocolLabel = (id: string | undefined) => PROTOCOLS.find((p) => p.id === (id ?? 'openai_chat'))?.label ?? id
const SESSION_HEADER = /^[A-Za-z][A-Za-z0-9-]{0,63}$/

function readForm(form: HTMLFormElement, adding: boolean): { body: ProviderProfileCreate } | { error: string } {
  const f = new FormData(form)
  const text = (k: string) => String(f.get(k) ?? '').trim()
  const name = text('name')
  const base_url = text('base_url')
  // A pasted key often carries a newline, which the provider answers with a 401 that looks like a wrong key.
  const api_key = text('api_key')
  if (!name) return { error: 'Enter a name.' }
  if (!/^https?:\/\/[^\s]+$/.test(base_url)) return { error: 'Enter the base URL with http:// or https://.' }
  if (adding && !api_key) return { error: 'Enter the API key.' }
  const pin = text('price_in')
  const pout = text('price_out')
  if ((pin === '') !== (pout === '')) return { error: 'Enter both prices, or neither.' }
  const prices = [Number(pin), Number(pout)]
  if (pin !== '' && prices.some((p) => !Number.isFinite(p) || p < 0)) return { error: 'Prices must be numbers of 0 or more.' }
  const conc = Number(text('max_concurrency') || 2)
  if (!Number.isInteger(conc) || conc < 1) return { error: 'Parallel calls must be a whole number of 1 or more.' }
  const window5h = text('max_calls_per_5h')
  if (window5h !== '' && !(Number.isInteger(Number(window5h)) && Number(window5h) >= 1)) return { error: 'Calls per 5 hours must be a whole number of 1 or more, or empty.' }
  const session = text('session_header')
  if (session !== '' && !SESSION_HEADER.test(session)) return { error: 'The session header must be a header name: a letter, then letters, digits or hyphens (64 at most).' }
  return {
    body: {
      name,
      base_url,
      api_key,
      default_model: text('default_model') || undefined,
      pricing: pin === '' ? undefined : { input_usd_per_mtok: prices[0]!, output_usd_per_mtok: prices[1]! },
      max_concurrency: conc,
      supports_seed: f.has('supports_seed'),
      protocol: text('protocol') as Protocol,
      max_calls_per_5h: window5h === '' ? null : Number(window5h),
      session_header: session || null,
    },
  }
}

function ProfileDialog({ profile, onClose }: { profile: ProviderProfile | null; onClose: () => void }) {
  const adding = profile === null
  const qc = useQueryClient()
  const models = useProfileModels(profile?.id ?? null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  // The key stays in the uncontrolled input until submit. It never enters React state, the query cache or a log.
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    const form = e.currentTarget
    const read = readForm(form, adding)
    if ('error' in read) return setError(read.error)
    setBusy(true)
    setError(null)
    try {
      const { api_key, ...rest } = read.body
      if (adding) await createProfile(read.body)
      else await updateProfile(profile.id, api_key ? read.body : rest)
      form.reset()
      await qc.invalidateQueries({ queryKey: PROFILES_KEY })
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'The request failed.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-h-[90dvh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{adding ? 'Add provider profile' : `Edit ${profile.name}`}</DialogTitle>
          <DialogDescription>
            A model endpoint in one of the protocols below. The backend stores the key in the OS keyring (only on a server with SIM_KEYRING=1) and never sends it back. This page shows only a masked hint. The default path is a SIM_PROFILE_* profile in the server .env.
          </DialogDescription>
        </DialogHeader>
        {isMock() && <NoKeyStore reason="mock" />}
        <form onSubmit={submit} className="space-y-3" autoComplete="off" noValidate>
          <div className="space-y-1">
            <Label htmlFor="pf-name">Name</Label>
            <Input id="pf-name" name="name" autoComplete="off" maxLength={80} defaultValue={profile?.name} required />
          </div>
          <div className="space-y-1">
            <Label htmlFor="pf-url">Base URL</Label>
            <Input id="pf-url" name="base_url" autoComplete="off" inputMode="url" placeholder="https://api.openai.com/v1" defaultValue={profile?.base_url} required />
          </div>
          <div className="space-y-1">
            <Label htmlFor="pf-protocol">Protocol</Label>
            <NativeSelect id="pf-protocol" name="protocol" defaultValue={profile?.protocol ?? 'openai_chat'}>
              {PROTOCOLS.map((p) => (
                <option key={p.id} value={p.id} disabled={!p.built}>
                  {p.label}
                </option>
              ))}
            </NativeSelect>
          </div>
          <div className="space-y-1">
            <Label htmlFor="pf-key">
              {adding ? 'API key' : 'New API key'}
              {!adding && <span className="font-normal text-muted-foreground"> (empty keeps {profile.key_hint ?? 'the current key'})</span>}
            </Label>
            <Input id="pf-key" name="api_key" type="password" autoComplete="off" spellCheck={false} data-1p-ignore data-lpignore="true" required={adding} />
            <p className="text-xs text-muted-foreground">Sent once to the backend. The browser does not keep it.</p>
          </div>
          <div className="space-y-1">
            <Label htmlFor="pf-model">Default model</Label>
            <Input id="pf-model" name="default_model" autoComplete="off" list="pf-models" defaultValue={profile?.default_model ?? ''} placeholder="gpt-4o-mini" />
            <datalist id="pf-models">
              {models.data?.models.map((m) => <option key={m} value={m} />)}
            </datalist>
          </div>
          <fieldset className="grid grid-cols-2 gap-3">
            <legend className="col-span-2 mb-1 text-sm font-medium">Price, USD per million tokens</legend>
            <div className="space-y-1">
              <Label htmlFor="pf-pin" className="font-normal">
                Input
              </Label>
              <Input id="pf-pin" name="price_in" type="number" min={0} step="any" defaultValue={profile?.pricing?.input_usd_per_mtok} />
            </div>
            <div className="space-y-1">
              <Label htmlFor="pf-pout" className="font-normal">
                Output
              </Label>
              <Input id="pf-pout" name="price_out" type="number" min={0} step="any" defaultValue={profile?.pricing?.output_usd_per_mtok} />
            </div>
            <p className="col-span-2 text-xs text-muted-foreground">Optional. The endpoint does not report prices. Without them the cost of a call is not counted, so a run with this profile needs a call or token cap.</p>
          </fieldset>
          <div className="space-y-1">
            <Label htmlFor="pf-conc">Parallel calls</Label>
            <Input id="pf-conc" name="max_concurrency" type="number" min={1} step={1} defaultValue={profile?.max_concurrency ?? 2} />
          </div>
          <div className="space-y-1">
            <Label htmlFor="pf-window">Calls per 5 hours</Label>
            <Input id="pf-window" name="max_calls_per_5h" type="number" min={1} step={1} defaultValue={profile?.max_calls_per_5h ?? ''} placeholder="no limit" />
            <p className="text-xs text-muted-foreground">Counted on this machine only. A full window stops the run (end reason rate_window).</p>
          </div>
          <div className="space-y-1">
            <Label htmlFor="pf-session">Session header</Label>
            <Input id="pf-session" name="session_header" autoComplete="off" spellCheck={false} maxLength={64} defaultValue={profile?.session_header ?? ''} placeholder="x-opencode-session (OpenCode Go)" />
            <p className="text-xs text-muted-foreground">A header name, not a secret. Each call sends it with the value run id:agent id. Empty sends no session header.</p>
          </div>
          <div className="space-y-1">
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" name="supports_seed" className="size-4" defaultChecked={profile?.supports_seed ?? false} />
              The endpoint honours the <code className="font-mono">seed</code> parameter
            </label>
            <p className="text-xs text-muted-foreground">When on, each call sends a seed derived from the run seed, seller, round, sample and attempt.</p>
          </div>
          <ErrorText>{error}</ErrorText>
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={busy}>
              {adding ? 'Add profile' : 'Save'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

function DeleteDialog({ profile, onClose }: { profile: ProviderProfile; onClose: () => void }) {
  const qc = useQueryClient()
  const del = useMutation({
    mutationFn: () => deleteProfile(profile.id),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: PROFILES_KEY })
      onClose()
    },
  })
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Delete {profile.name}?</DialogTitle>
          <DialogDescription>The backend deletes the profile and its stored key. Past runs keep the profile id as a reference.</DialogDescription>
        </DialogHeader>
        <ErrorText>{del.error?.message ?? null}</ErrorText>
        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="destructive" disabled={del.isPending} onClick={() => del.mutate()}>
            Delete
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function yesNo(v: boolean | null | undefined) {
  return v === true ? 'yes' : v === false ? 'no' : 'not tested'
}

function TestResult({ r }: { r: ProfileTestResult }) {
  if (r.ok) {
    return (
      <p className="text-sm text-emerald-700 dark:text-emerald-400">
        OK with {r.model} in {r.latency_ms ?? '?'} ms. Tool calls: {yesNo(r.supports?.tool_calls)}. JSON schema: {yesNo(r.supports?.json_schema)}.
      </p>
    )
  }
  // The provider's own words come second: our summary says what failed, the provider says why (contract 0.34.0).
  const said = [r.error?.provider_code, r.error?.provider_message].filter(Boolean).join(': ')
  return (
    <p role="alert" className="space-y-0.5 rounded-md border border-red-500/40 bg-red-500/10 p-2 text-sm">
      <span className="block">{`Failed with ${r.model}: ${r.error?.kind ?? 'other'}${r.error?.http_status ? ` (HTTP ${r.error.http_status})` : ''}. ${r.error?.message ?? ''}`}</span>
      {said && <span className="block">{`The provider said: ${said}`}</span>}
    </p>
  )
}

function ProfileCard({ p, onEdit, onDelete }: { p: ProviderProfile; onEdit: () => void; onDelete: () => void }) {
  const qc = useQueryClient()
  const test = useMutation({ mutationFn: () => testProfile(p.id), onSuccess: () => qc.invalidateQueries({ queryKey: PROFILES_KEY }) })
  const price = p.pricing ? `${p.pricing.input_usd_per_mtok} in, ${p.pricing.output_usd_per_mtok} out` : null
  return (
    <li className="space-y-3 rounded-lg border p-4">
      <div className="flex flex-wrap items-start gap-2">
        <div className="min-w-0 flex-1">
          <h2 className="font-medium">
            {p.name}{' '}
            {p.source === 'env' && <span className="rounded border px-1.5 text-xs font-normal text-muted-foreground">.env, read-only</span>}
          </h2>
          <p className="font-mono text-xs break-all text-muted-foreground">{p.base_url}</p>
        </div>
        <div className="flex gap-1">
          <Button size="sm" variant="outline" disabled={test.isPending} onClick={() => test.mutate()} title="Makes one small call to the endpoint. This costs one call.">
            <Plug /> {test.isPending ? 'Testing' : 'Test (1 call)'}
          </Button>
          {!p.read_only && (
            <>
              <Button size="icon" variant="ghost" className="size-8" aria-label={`Edit ${p.name}`} onClick={onEdit}>
                <Pencil />
              </Button>
              <Button size="icon" variant="ghost" className="size-8" aria-label={`Delete ${p.name}`} onClick={onDelete}>
                <Trash2 />
              </Button>
            </>
          )}
        </div>
      </div>
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
        <dt className="text-muted-foreground">Key</dt>
        <dd className="flex flex-wrap items-center gap-x-1.5">
          <KeyRound className="size-3.5 text-muted-foreground" aria-hidden />
          {p.key_set ? <span className="font-mono whitespace-nowrap">{p.key_hint ?? 'set'}</span> : <span className="text-red-600 dark:text-red-400">missing</span>}
          {p.key_store === 'env' ? (
            <span className="text-xs text-muted-foreground">
              from <code className="font-mono break-all">{p.key_env}</code> in the server .env (read-only)
            </span>
          ) : (
            <span className="text-xs text-muted-foreground">
              in the OS keyring (a server with SIM_KEYRING=1 is needed). Or set <code className="font-mono break-all">{p.key_env}</code> in the server .env; the keyring key wins.
            </span>
          )}
        </dd>
        <dt className="text-muted-foreground">Protocol</dt>
        <dd>{protocolLabel(p.protocol)}</dd>
        <dt className="text-muted-foreground">Default model</dt>
        <dd>{p.default_model ?? 'none'}</dd>
        <dt className="text-muted-foreground">Price (USD / M tokens)</dt>
        <dd>{price ?? <span className="text-amber-700 dark:text-amber-400">not set: cost not counted; runs need a call or token cap</span>}</dd>
        <dt className="text-muted-foreground">Tool calls</dt>
        <dd>
          {yesNo(p.supports?.tool_calls)}
          {p.supports?.checked_at && <span className="text-xs text-muted-foreground"> (tested {new Date(p.supports.checked_at).toLocaleString()})</span>}
        </dd>
        <dt className="text-muted-foreground">Parallel calls</dt>
        <dd>{p.max_concurrency ?? 2}</dd>
        <dt className="text-muted-foreground">Seed parameter</dt>
        <dd>{p.supports_seed ? 'sent with each call' : 'not sent'}</dd>
        <dt className="text-muted-foreground">5-hour window</dt>
        <dd>
          {p.rate_window ? (
            <span className={p.rate_window.room === 0 ? 'text-amber-700 dark:text-amber-400' : undefined}>
              {p.rate_window.used} of {p.rate_window.max_calls_per_5h} calls used, {p.rate_window.room} left (this machine, calls in flight included)
              {p.rate_window.rate_limited ? `, ${p.rate_window.rate_limited} rate-limited` : ''}
            </span>
          ) : (
            'no limit'
          )}
        </dd>
        <dt className="text-muted-foreground">Session header</dt>
        <dd>
          {p.session_header ? <code className="font-mono">{p.session_header}</code> : 'none'}
          <span className="block text-xs text-muted-foreground">Every call also sends User-Agent collusion-sim/&lt;version&gt;.</span>
        </dd>
      </dl>
      {p.read_only && <p className="text-xs text-muted-foreground">This profile comes from the server .env. Change it there and restart the server. The test call works here.</p>}
      {test.data && <TestResult r={test.data} />}
      <ErrorText>{test.error?.message ?? null}</ErrorText>
    </li>
  )
}

type Open = { kind: 'add' } | { kind: 'edit'; p: ProviderProfile } | { kind: 'delete'; p: ProviderProfile } | null

export function ProvidersPage() {
  const profiles = useProfiles()
  const [open, setOpen] = useState<Open>(null)
  const close = () => setOpen(null)
  const noStore = isMock() ? 'mock' : profiles.error instanceof ApiError && (profiles.error.status === 404 || profiles.error.code === 'not_built') ? 'missing' : null

  return (
    <div className="max-w-3xl space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <div className="min-w-0 flex-1">
          <h1 className="text-xl font-semibold">Provider profiles</h1>
          <p className="text-sm text-muted-foreground">Each LLM agent selects a profile and a model. A profile is an endpoint and its key.</p>
        </div>
        <Button onClick={() => setOpen({ kind: 'add' })} disabled={noStore === 'missing'}>
          <Plus /> Add profile
        </Button>
      </div>

      {noStore && <NoKeyStore reason={noStore} />}
      <ErrorText>{profiles.error?.message ?? null}</ErrorText>
      {profiles.isPending && <Skeleton className="h-40 w-full" />}
      {profiles.data?.length === 0 && (
        <div className="rounded-lg border border-dashed p-6 text-center">
          <p className="font-medium">No provider profiles yet.</p>
          <p className="text-sm text-muted-foreground">Add one before you start an LLM run. Scripted runs need no profile.</p>
        </div>
      )}
      <ul className="space-y-3">
        {profiles.data?.map((p) => (
          <ProfileCard key={p.id} p={p} onEdit={() => setOpen({ kind: 'edit', p })} onDelete={() => setOpen({ kind: 'delete', p })} />
        ))}
      </ul>

      {(open?.kind === 'add' || open?.kind === 'edit') && <ProfileDialog profile={open.kind === 'edit' ? open.p : null} onClose={close} />}
      {open?.kind === 'delete' && <DeleteDialog profile={open.p} onClose={close} />}
    </div>
  )
}
