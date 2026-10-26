import { useNavigate } from 'react-router'
import { useScenario, useScenarioReference } from '@/api/scenarios'
import { Skeleton } from '@/components/ui/skeleton'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { AgentsEditor } from '@/features/new-run/AgentsEditor'
import { PresetBar } from '@/features/new-run/PresetBar'
import { Form } from '@/features/new-run/SchemaField'
import { StartBar, StartDialog, useRunDraft, type Kind, type RunDraft } from '@/features/new-run/useRunDraft'
import { ScenarioChart } from '@/features/scenarios/ScenarioChart'
import { InfoHint } from '@/components/InfoHint'

/** The scenario a run of this draft plays: the chosen one, or for research the one of the first chosen seed (seed map). */
function previewScenario(d: RunDraft): { id: string; why: string } | null {
  if (!d.research) return d.draft.scenario_id ? { id: d.draft.scenario_id, why: 'the chosen scenario' } : null
  const pool = d.seedMap?.seeds ?? []
  const seed = d.draft.seeds[0]
  const entry = pool.find((s) => s.seed === seed) ?? pool.find((s) => s.split === d.draft.split)
  return entry ? { id: entry.scenario_id, why: `seed ${entry.seed}; each seed has its own scenario` } : null
}

function ScenarioPreview({ d }: { d: RunDraft }) {
  const which = previewScenario(d)
  const ref = useScenarioReference(which?.id)
  return (
    <details className="min-w-0 rounded-md border p-2" aria-label="Market preview">
      <summary className="cursor-pointer text-xs font-medium">Market preview</summary>
      <div className="mt-2 space-y-2">
      {!which ? (
        <p className="text-xs text-muted-foreground">Choose a scenario or a seed to see its Nash and joint-optimum prices per round.</p>
      ) : ref.error ? (
        <p role="alert" className="text-xs text-red-600 dark:text-red-400">
          No preview of scenario {which.id} ({which.why}): {ref.error.message}
        </p>
      ) : ref.data ? (
        <>
          <ScenarioChart rounds={ref.data.rounds} markers={ref.data.markers} />
          <p className="text-xs text-muted-foreground">
            Scenario {which.id} ({which.why}).{ref.data.references_available ? '' : ' This demand model has no Nash or joint-optimum values.'}
          </p>
        </>
      ) : (
        <Skeleton className="h-[160px] w-full" />
      )}
      </div>
    </details>
  )
}

/** The news events of the previewed scenario, for the Host card. The host posts them; the scenario sets them. */
function HostNews({ d }: { d: RunDraft }) {
  const which = previewScenario(d)
  const sc = useScenario(which?.id)
  if (!which) return null
  const events = sc.data?.events ?? []
  return (
    <div className="space-y-1">
      <p className="text-xs font-medium">News · {which.id}</p>
      {sc.error ? (
        <p className="text-xs text-muted-foreground">Not available: {sc.error.message}</p>
      ) : !sc.data ? (
        <Skeleton className="h-10 w-full" />
      ) : events.length === 0 ? (
        <p className="text-xs text-muted-foreground">This scenario has no news events.</p>
      ) : (
        <ul className="space-y-1 text-xs">
          {events.map((e, i) => (
            <li key={i} className="rounded-md bg-muted/50 px-2 py-1">
              <span className="font-mono">R{e.announce}</span> {e.news || '(no text)'}{' '}
              <span className="text-muted-foreground">
                (seen by {e.visible.length ? e.visible.join(', ') : 'nobody'}
                {e.kind === 'param_shock' && e.effect != null ? `; shock from R${e.effect}` : ''})
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

/**
 * Workbench setup mode (UI-WORKBENCH.md §2, §6 step 3): the New run form laid out as the workbench. The agents are cards,
 * a `+` card adds a seller, the Host card lists the scenario's news, the market chart previews the scenario, and the Setting panel holds the other fields, the estimate and Start.
 * A frozen condition makes its fields read only; Start goes through the server gates and shows their refusal.
 */
export function SetupPage() {
  const navigate = useNavigate()
  const d = useRunDraft((ids, kind) => navigate(ids.length === 1 ? `/workbench/${ids[0]}` : `/runs?kind=${kind}`))

  if (d?.loadError) {
    return (
      <p role="alert" className="rounded-md border border-red-500/40 bg-red-500/10 p-3 text-sm">
        {d.loadError.message}
      </p>
    )
  }
  if (!d?.reg) return <Skeleton className="h-96 w-full" />

  const settings = d.fieldNames.filter((k) => k !== 'agents')
  return (
    <Form.Provider value={d.ctx}>
      <form className="space-y-3 pb-8" onSubmit={(e) => e.preventDefault()} noValidate>
        <header className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-1">
            <h1 className="text-lg font-semibold">New run</h1>
            <InfoHint label="About this run kind">
              {d.research
                ? 'Research: the condition sets the labels, channels, prompts and tools. Those fields are read only.'
                : 'Exploratory: no labels, every setting is free.'}
            </InfoHint>
          </div>
          <ToggleGroup type="single" size="sm" variant="outline" value={d.kind} onValueChange={(v) => v && d.chooseKind(v as Kind)} aria-label="Run kind">
            <ToggleGroupItem value="exploratory">Exploratory</ToggleGroupItem>
            <ToggleGroupItem value="research">Research</ToggleGroupItem>
          </ToggleGroup>
        </header>
        <PresetBar draft={d.request} onLoad={d.loadPreset} />

        <div key={d.rev} className="grid gap-2 xl:grid-cols-[minmax(0,1fr)_22rem]">
          <div className="min-w-0 space-y-2">
            <section aria-label="Agents" className="space-y-1.5">
              <h2 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Agents</h2>
              <AgentsEditor agents={d.draft.agents} request={d.request} reg={d.reg} condition={d.condition} grid hostNews={<HostNews d={d} />} />
            </section>
            <ScenarioPreview d={d} />
          </div>
          <section aria-label="Setting" className="min-w-0 space-y-2 self-start rounded-md border p-2 xl:sticky xl:top-2">
            <h2 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Setting</h2>
            <div className="space-y-2 [&_fieldset]:space-y-1.5 [&_fieldset]:p-2 [&_legend]:text-xs">{settings.map(d.field)}</div>
            {d.note && <p className="text-xs text-amber-700 dark:text-amber-400">{d.note}</p>}
            <div className="sticky bottom-0 z-10 -mx-2 border-t bg-background/95 px-2 py-2 backdrop-blur">
              <StartBar d={d} />
            </div>
          </section>
        </div>
      </form>
      <StartDialog d={d} />
    </Form.Provider>
  )
}
