import { useEffect, useRef, type ReactNode } from 'react'
import { useNavigate } from 'react-router'
import { runPath } from '@/lib/paths'
import { Skeleton } from '@/components/ui/skeleton'
import { InfoHint } from '@/components/InfoHint'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { PresetBar } from './PresetBar'
import { Form } from './SchemaField'
import { StartBar, StartDialog, useRunDraft, type Kind } from './useRunDraft'

/** Where each top-level field goes. A field that the schema adds later lands in "Other settings". */
const SECTIONS: { title: string; fields: string[] }[] = [
  { title: 'Setup', fields: ['name', 'scenario_id', 'condition_id', 'prompt_variant_id', 'split', 'seeds'] },
  { title: 'Agents', fields: ['agents'] },
  { title: 'Market rules', fields: ['channels', 'transparency', 'call_mode', 'observation_mode', 'clock', 'limits'] },
  { title: 'Memory, budget and notes', fields: ['memory', 'budget', 'batch_budget', 'snapshot_every', 'notes'] },
  { title: 'Run view', fields: ['indicators'] },
]

const sectionId = (title: string) => `sec-${title.toLowerCase().replace(/[^a-z]+/g, '-')}`

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section id={sectionId(title)} className="scroll-mt-4 space-y-3 rounded-xl border p-4">
      <h2 className="font-medium">{title}</h2>
      {children}
    </section>
  )
}

export function NewRunPage() {
  const navigate = useNavigate()
  const d = useRunDraft((ids, kind) => navigate(ids.length === 1 ? runPath(ids[0]!) : `/runs?kind=${kind}`))
  // The draft lives in memory only. A reload or a closed tab drops it, so the browser asks first once the form differs from its first state.
  const first = useRef<string | null>(null)
  const now = d?.reg ? JSON.stringify(d.request) : null
  if (first.current === null && now !== null) first.current = now
  const dirty = now !== null && first.current !== now
  useEffect(() => {
    if (!dirty) return
    const warn = (e: BeforeUnloadEvent) => e.preventDefault()
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [dirty])

  if (d?.loadError) {
    return (
      <p role="alert" className="rounded-md border border-red-500/40 bg-red-500/10 p-3 text-sm">
        {d.loadError.message}
      </p>
    )
  }
  if (!d?.reg) return <Skeleton className="h-96 w-full max-w-4xl" />

  const placed = new Set(SECTIONS.flatMap((s) => s.fields))
  const other = d.fieldNames.filter((k) => !placed.has(k))

  return (
    <Form.Provider value={d.ctx}>
      <form className="min-w-0 max-w-4xl space-y-4 pb-24" onSubmit={(e) => e.preventDefault()} noValidate>
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div className="flex items-center gap-1">
            <h1 className="text-xl font-semibold">New run</h1>
            <InfoHint label="About this run kind">
              {d.research
                ? 'A research run takes its labels, channels, prompts and tools from the condition. Those fields are read-only here.'
                : 'An exploratory run has no labels. Every setting is free.'}
            </InfoHint>
          </div>
          <ToggleGroup type="single" variant="outline" value={d.kind} onValueChange={(v) => v && d.chooseKind(v as Kind)} aria-label="Run kind">
            <ToggleGroupItem value="exploratory">Exploratory</ToggleGroupItem>
            <ToggleGroupItem value="research">Research</ToggleGroupItem>
          </ToggleGroup>
        </div>

        <PresetBar draft={d.request} onLoad={d.loadPreset} />
        {dirty && (
          <p className="flex items-center gap-1 text-xs text-muted-foreground">
            Unsaved setup
            <InfoHint label="About unsaved setup">This setup is not saved. A reload drops it. Save it as a preset to keep it.</InfoHint>
          </p>
        )}
        <nav aria-label="Sections" className="sticky top-0 z-10 -mx-4 flex flex-wrap gap-x-3 gap-y-1 border-b bg-background/95 px-4 py-2 text-sm backdrop-blur">
          {[...SECTIONS.filter((s) => s.fields.some((f) => d.fieldNames.includes(f))).map((s) => s.title), ...(other.length > 0 ? ['Other settings'] : [])].map((t) => (
            <a key={t} href={`#${sectionId(t)}`} className="text-muted-foreground hover:text-foreground hover:underline" onClick={(e) => { e.preventDefault(); document.getElementById(sectionId(t))?.scrollIntoView({ block: 'start' }) }}>
              {t}
            </a>
          ))}
        </nav>

        <div key={d.rev} className="space-y-4">
          {SECTIONS.map((s) => {
            const body = s.fields.map(d.field).filter(Boolean)
            return body.length > 0 && <Section key={s.title} title={s.title}>{body}</Section>
          })}
          {other.length > 0 && <Section title="Other settings">{other.map(d.field)}</Section>}
        </div>
        {d.note && <p className="text-sm text-amber-700 dark:text-amber-400">{d.note}</p>}

        <div className="sticky bottom-0 z-10 -mx-4 border-t bg-background/95 px-4 py-3 backdrop-blur">
          <StartBar d={d} />
        </div>
      </form>
      <StartDialog d={d} />
    </Form.Provider>
  )
}
