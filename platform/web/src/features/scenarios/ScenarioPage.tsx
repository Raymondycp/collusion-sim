import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Copy, ListChecks, Lock, Save, Sparkles, Trash2 } from 'lucide-react'
import { ApiError, asApiError } from '@/api/client'
import {
  checkScenario,
  createScenario,
  deleteScenario,
  freezeScenario,
  previewMarket,
  replaceScenario,
  useDemandModels,
  useScenario,
  useScenarioReference,
  type CheckReport,
  type DemandModel,
  type Scenario,
  type ScenarioDraft,
} from '@/api/scenarios'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { NativeSelect } from '@/features/new-run/SchemaField'
import { CheckList } from './CheckList'
import { GenerateDialog } from './GenerateDialog'
import { markersFor, sellerIds, starterDraft, toDraft } from './scenario'
import { ScenarioChart } from './ScenarioChart'
import { ScenarioEditor, Section } from './ScenarioEditor'
import { StatusBadge } from './ScenariosPage'

type Failure = { message: string; fields: Record<string, string> }
const failure = (e: unknown): Failure => {
  const a = asApiError(e)
  return { message: a.message, fields: Object.fromEntries(a.fields.map((f) => [f.path, f.message])) }
}

function useDebounced<T>(value: T, ms: number) {
  const [v, setV] = useState(value)
  useEffect(() => {
    const id = setTimeout(() => setV(value), ms)
    return () => clearTimeout(id)
  }, [value, ms])
  return v
}

function Back() {
  return (
    <Link to="/scenarios" className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground">
      <ArrowLeft className="size-4" /> Scenarios
    </Link>
  )
}

function FailureText({ f }: { f: Failure | null }) {
  if (!f) return null
  const fields = Object.entries(f.fields)
  return (
    <div role="alert" className="space-y-0.5 text-sm text-red-600 dark:text-red-400">
      <p>{f.message}</p>
      {fields.map(([k, v]) => (
        <p key={k} className="text-xs">
          <span className="font-mono">{k || 'draft'}</span>: {v}
        </p>
      ))}
    </div>
  )
}

/** The draft's reference prices before any run: POST /market/preview, a pure calculation. */
function DraftPreview({ draft }: { draft: ScenarioDraft }) {
  const body = useDebounced(draft, 400)
  const preview = useQuery({ queryKey: ['market-preview', JSON.stringify(body)], queryFn: () => previewMarket(body), placeholderData: (prev) => prev, retry: false })
  return (
    <Section title="Preview">
      {preview.error ? (
        <p role="alert" className="text-sm text-red-600 dark:text-red-400">
          No preview: {preview.error.message}
          {preview.error instanceof ApiError && preview.error.fields.length > 0 && ` ${preview.error.fields.map((f) => `${f.path}: ${f.message}`).join('; ')}`}
        </p>
      ) : preview.data ? (
        <>
          <ScenarioChart rounds={preview.data.rounds} markers={markersFor(body.events)} quietWindow={body.quiet_window} />
          <CheckList checks={preview.data.checks} title="Preview checks" />
          <p className="text-xs text-muted-foreground">The preview covers {body.horizon.max_rounds} rounds (the maximum). The quiet-window check runs with Check, on the saved draft.</p>
        </>
      ) : (
        <Skeleton className="h-[300px] w-full" />
      )}
    </Section>
  )
}

function Facts({ s }: { s: Scenario }) {
  const d = s.market.demand
  const rows: [string, string][] = [
    ['Id', s.id],
    ['Hash', s.hash ?? 'none'],
    ['Approved by', s.approved_by ?? 'none'],
    ['Frozen at', s.frozen_at ? new Date(s.frozen_at).toLocaleString() : 'not frozen'],
    ['Demand model', `${d.id} v${d.version ?? '?'}`],
    ['Params', Object.entries(d.params).map(([k, v]) => `${k} = ${typeof v === 'number' ? v : JSON.stringify(v)}`).join(', ')],
    ['Price grid', s.market.grid ? `${s.market.grid.min} to ${s.market.grid.max}, step ${s.market.grid.step}` : 'default'],
    ['Sellers', sellerIds(s).join(', ')],
    ['Rounds', `${s.horizon.min_rounds} to ${s.horizon.max_rounds}, stop chance ${s.horizon.stop_prob}`],
    ['Quiet window', s.quiet_window?.length === 2 ? `R${s.quiet_window[0]} to R${s.quiet_window[1]}` : 'none'],
  ]
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
      {rows.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-muted-foreground">{k}</dt>
          <dd className="font-mono text-xs break-all">{v}</dd>
        </div>
      ))}
    </dl>
  )
}

function EventsTable({ s }: { s: ScenarioDraft }) {
  if (s.events.length === 0) return <p className="text-sm text-muted-foreground">No events.</p>
  return (
    <div className="overflow-x-auto rounded-lg border">
      <table className="w-full text-left text-sm">
        <thead className="bg-muted/50 text-xs text-muted-foreground">
          <tr>
            <th className="p-2">#</th>
            <th className="p-2">News</th>
            <th className="p-2">Shock</th>
            <th className="p-2">Change</th>
            <th className="p-2">Seen by</th>
            <th className="p-2">News text</th>
          </tr>
        </thead>
        <tbody>
          {s.events.map((e, i) => (
            <tr key={i} className="border-t align-top">
              <td className="p-2">{i + 1}</td>
              <td className="p-2">R{e.announce}</td>
              <td className="p-2 whitespace-nowrap">{e.kind === 'news_only' ? 'none (placebo)' : `R${e.effect} for ${e.length}`}</td>
              <td className="p-2 font-mono text-xs">{(e.shocks ?? []).map((x) => `${x.path.slice(1)} ${x.op} ${x.value}`).join(', ') || 'none'}</td>
              <td className="p-2">{e.visible.join(', ')}</td>
              <td className="p-2">
                {e.news}
                <span className="block text-xs text-muted-foreground">
                  {e.news_source === 'ai' ? 'AI text' : 'human text'}
                  {e.misleading ? ', misleading on purpose' : ''}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function FrozenView({ s }: { s: Scenario }) {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const ref = useScenarioReference(s.id)
  const [error, setError] = useState<Failure | null>(null)
  const clone = async () => {
    setError(null)
    try {
      const copy = await createScenario({ ...toDraft(s), name: `${s.name} (copy)`, provenance: { cloned_from: s.id, cloned_from_hash: s.hash, source_provenance: s.provenance ?? null } })
      await qc.invalidateQueries({ queryKey: ['scenarios'] })
      navigate(`/scenarios/${copy.id}`)
    } catch (e) {
      setError(failure(e))
    }
  }
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-lg font-semibold">{s.name}</h1>
        <StatusBadge status={s.status} />
        <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
          <Lock className="size-3" aria-hidden /> read-only
        </span>
        <Button size="sm" variant="outline" className="ml-auto" onClick={() => void clone()}>
          <Copy /> Clone to draft
        </Button>
      </div>
      <FailureText f={error} />
      {s.description && <p className="text-sm">{s.description}</p>}
      <Section title="Reference prices">
        {ref.data ? (
          <ScenarioChart rounds={ref.data.rounds} markers={ref.data.markers} quietWindow={s.quiet_window} />
        ) : ref.error ? (
          <p role="alert" className="text-sm text-red-600 dark:text-red-400">
            {ref.error.message}
          </p>
        ) : (
          <Skeleton className="h-[300px] w-full" />
        )}
        {ref.data && !ref.data.references_available && <p className="text-sm text-amber-700 dark:text-amber-400">This demand model has no Nash or joint-optimum values.</p>}
      </Section>
      <Section title="Setup">
        <Facts s={s} />
      </Section>
      <Section title="Events">
        <EventsTable s={s} />
      </Section>
      <Section title="Provenance">
        <pre className="overflow-x-auto rounded-md bg-muted/50 p-2 text-xs">{s.provenance ? JSON.stringify(s.provenance, null, 2) : 'Written by a person (no generator).'}</pre>
      </Section>
    </div>
  )
}

function DraftView({ s, models }: { s: Scenario; models: DemandModel[] }) {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const saved = toDraft(s)
  const [draft, setDraft] = useState(saved)
  const [error, setError] = useState<Failure | null>(null)
  const [report, setReport] = useState<CheckReport | null>(null)
  const [approver, setApprover] = useState('')
  const [busy, setBusy] = useState(false)
  const [gen, setGen] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const dirty = JSON.stringify(draft) !== JSON.stringify(saved)

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true)
    setError(null)
    try {
      await fn()
    } catch (e) {
      setError(failure(e))
    } finally {
      setBusy(false)
    }
  }
  const store = async (d: ScenarioDraft) => {
    const next = await replaceScenario(s.id, d)
    qc.setQueryData(['scenario', s.id], next)
    setDraft(toDraft(next))
    setReport(null)
    await qc.invalidateQueries({ queryKey: ['scenarios'] })
  }
  const save = () => act(() => store(draft))
  const check = () => act(async () => setReport(await checkScenario(s.id)))
  const freeze = () =>
    act(async () => {
      const next = await freezeScenario(s.id, approver.trim())
      qc.setQueryData(['scenario', s.id], next)
      await qc.invalidateQueries({ queryKey: ['scenarios'] })
    })
  const remove = () =>
    act(async () => {
      await deleteScenario(s.id)
      await qc.invalidateQueries({ queryKey: ['scenarios'] })
      navigate('/scenarios')
    })

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-lg font-semibold">{draft.name || s.id}</h1>
        <StatusBadge status="draft" />
        {dirty && <span className="text-xs text-amber-700 dark:text-amber-400">unsaved changes</span>}
        <div className="ml-auto flex flex-wrap gap-2">
          <Button size="sm" variant="outline" onClick={() => setGen(true)}>
            <Sparkles /> Generate
          </Button>
          <Button size="sm" onClick={() => void save()} disabled={!dirty || busy}>
            <Save /> Save
          </Button>
        </div>
      </div>
      <FailureText f={error} />
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <ScenarioEditor draft={draft} onChange={setDraft} models={models} errors={error?.fields ?? {}} />
        <div className="space-y-4 lg:sticky lg:top-16 lg:self-start">
          <DraftPreview draft={draft} />
          <Section title="Check and freeze">
            <p className="text-xs text-muted-foreground">Saved edit {s.version}. Check and freeze use the saved draft. {dirty ? 'Save first.' : ''}</p>
            <Button size="sm" variant="outline" onClick={() => void check()} disabled={dirty || busy}>
              <ListChecks /> Check
            </Button>
            {report && <CheckList checks={report.checks} />}
            <div className="flex flex-wrap items-end gap-2">
              <div className="space-y-1">
                <label htmlFor="scn-approver" className="text-sm font-medium">
                  Approved by
                </label>
                <Input id="scn-approver" value={approver} onChange={(e) => setApprover(e.target.value)} placeholder="Your name" />
              </div>
              <Button size="sm" onClick={() => void freeze()} disabled={dirty || busy || !approver.trim()}>
                <Lock /> Freeze
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">Freeze runs the checks, computes the hash and makes the scenario read-only. Research runs use only frozen scenarios.</p>
          </Section>
          <Button size="sm" variant="ghost" onClick={() => setConfirmDelete(true)}>
            <Trash2 /> Delete draft
          </Button>
        </div>
      </div>
      <GenerateDialog
        open={gen}
        onOpenChange={setGen}
        current={draft}
        onUse={(d, keep) => {
          setDraft(d)
          if (keep) void act(() => store(d))
        }}
      />
      <Dialog open={confirmDelete} onOpenChange={setConfirmDelete}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete {draft.name || s.id}?</DialogTitle>
            <DialogDescription>The draft is removed from the server. This cannot be undone.</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setConfirmDelete(false)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={() => {
                setConfirmDelete(false)
                void remove()
              }}
            >
              Delete draft
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

/** A new scenario: start from a demand model or from a generator. Nothing is chosen for the person. */
function NewScenario({ models }: { models: DemandModel[] }) {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [draft, setDraft] = useState<ScenarioDraft | null>(null)
  const [gen, setGen] = useState(false)
  const [error, setError] = useState<Failure | null>(null)
  const [busy, setBusy] = useState(false)

  const create = async (d: ScenarioDraft) => {
    setBusy(true)
    setError(null)
    try {
      const s = await createScenario(d)
      qc.setQueryData(['scenario', s.id], s)
      await qc.invalidateQueries({ queryKey: ['scenarios'] })
      navigate(`/scenarios/${s.id}`, { replace: true })
    } catch (e) {
      setError(failure(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-lg font-semibold">New draft</h1>
        <div className="ml-auto flex gap-2">
          <Button size="sm" variant="outline" onClick={() => setGen(true)}>
            <Sparkles /> Generate
          </Button>
          {draft && (
            <Button size="sm" onClick={() => void create(draft)} disabled={busy}>
              <Save /> Save draft
            </Button>
          )}
        </div>
      </div>
      <FailureText f={error} />
      {!draft && (
        <Section title="Start">
          <div className="max-w-sm space-y-1">
            <label htmlFor="scn-start-model" className="text-sm font-medium">
              Start from a demand model
            </label>
            <NativeSelect
              id="scn-start-model"
              value=""
              onChange={(e) => {
                const m = models.find((x) => x.id === e.target.value)
                if (m) setDraft(starterDraft(m))
              }}
            >
              <option value="">Select…</option>
              {models.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name}
                </option>
              ))}
            </NativeSelect>
          </div>
          <p className="text-sm text-muted-foreground">Or use Generate to start from a template or a random draft.</p>
        </Section>
      )}
      {draft && (
        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
          <ScenarioEditor draft={draft} onChange={setDraft} models={models} errors={error?.fields ?? {}} />
          <div className="lg:sticky lg:top-16 lg:self-start">
            <DraftPreview draft={draft} />
          </div>
        </div>
      )}
      <GenerateDialog
        open={gen}
        onOpenChange={setGen}
        current={draft}
        onUse={(d, keep) => {
          setDraft(d)
          if (keep) void create(d)
        }}
      />
    </div>
  )
}

export function ScenarioPage() {
  const { scenarioId } = useParams()
  const models = useDemandModels()
  const isNew = scenarioId === undefined
  const scenario = useScenario(scenarioId)
  const s = isNew ? null : scenario.data

  const error = models.error ?? (isNew ? null : scenario.error)
  return (
    <div className="space-y-3">
      <Back />
      {error ? (
        <p role="alert" className="text-sm text-red-600 dark:text-red-400">
          {error.message}
        </p>
      ) : !models.data || (!isNew && !s) ? (
        <Skeleton className="h-64 w-full" />
      ) : isNew ? (
        <NewScenario models={models.data} />
      ) : s!.status === 'frozen' ? (
        <FrozenView s={s!} />
      ) : (
        <DraftView key={s!.id} s={s!} models={models.data} />
      )}
    </div>
  )
}
