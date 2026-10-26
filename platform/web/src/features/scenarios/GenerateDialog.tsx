import { useState } from 'react'
import { Sparkles } from 'lucide-react'
import { asApiError, type ApiError } from '@/api/client'
import { generateScenario, useGenerators, type GenerateResult, type ScenarioDraft, type ScenarioGenerator } from '@/api/scenarios'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { resolve } from '@/lib/jsonSchema'
import { NativeSelect } from '@/features/new-run/SchemaField'
import { CheckList } from './CheckList'
import { draftDiff } from './scenario'
import { SchemaForm } from './SchemaForm'

type Props = {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** The draft in the editor, for the diff. Null on a new scenario. */
  current: ScenarioDraft | null
  onUse: (draft: ScenarioDraft, save: boolean) => void
}

/** Only the defaults the generator states. A required choice with no default stays empty, so the person makes it. */
const schemaDefaults = (g: ScenarioGenerator) =>
  Object.fromEntries(Object.entries(g.input_schema.properties ?? {}).flatMap(([k, p]) => (resolve(g.input_schema, p).default === undefined ? [] : [[k, structuredClone(resolve(g.input_schema, p).default)]])))

const show = (v: unknown) => (v === undefined ? '' : typeof v === 'string' ? v : JSON.stringify(v))

/** Pick a generator, fill its inputs, see the draft, its check and the diff. Accept, edit or discard; nothing is saved before that. */
export function GenerateDialog({ open, onOpenChange, current, onUse }: Props) {
  const generators = useGenerators()
  const [gen, setGen] = useState<ScenarioGenerator | null>(null)
  const [inputs, setInputs] = useState<Record<string, unknown>>({})
  const [seed, setSeed] = useState(0)
  const [result, setResult] = useState<GenerateResult | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [busy, setBusy] = useState(false)

  const pick = (id: string) => {
    const g = generators.data?.find((x) => x.id === id) ?? null
    setGen(g)
    setInputs(g ? schemaDefaults(g) : {})
    setResult(null)
    setError(null)
  }
  const run = async () => {
    if (!gen) return
    setBusy(true)
    setError(null)
    setResult(null)
    try {
      setResult(await generateScenario({ generator_id: gen.id, inputs, seed }))
    } catch (e) {
      setError(asApiError(e))
    } finally {
      setBusy(false)
    }
  }
  const close = () => {
    setResult(null)
    onOpenChange(false)
  }
  const use = (save: boolean) => {
    if (!result) return
    onUse(result.draft, save)
    close()
  }
  const diff = result ? draftDiff(current, result.draft) : []
  const fieldErrors = Object.fromEntries((error?.fields ?? []).filter((f) => f.path.startsWith('inputs.')).map((f) => [f.path.slice('inputs.'.length), f.message]))

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Generate a draft</DialogTitle>
          <DialogDescription>A generator returns a draft and its check. Nothing is saved until you accept it.</DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div className="space-y-1">
            <label htmlFor="gen-id" className="text-sm font-medium">
              Generator
            </label>
            <NativeSelect id="gen-id" value={gen?.id ?? ''} onChange={(e) => pick(e.target.value)}>
              <option value="">{generators.isPending ? 'Loading…' : 'Select…'}</option>
              {generators.data?.map((g) => (
                <option key={g.id} value={g.id} disabled={g.paid}>
                  {g.name}
                  {g.paid ? ' (costs LLM calls: needs lead approval)' : ''}
                </option>
              ))}
            </NativeSelect>
            {generators.error && <p role="alert" className="text-sm text-red-600 dark:text-red-400">{generators.error.message}</p>}
            {gen?.description && <p className="text-xs text-muted-foreground">{gen.description}</p>}
          </div>
          {gen && (
            <>
              <SchemaForm id="inputs" legend="Inputs" schema={gen.input_schema} value={inputs} onChange={setInputs} errors={fieldErrors} />
              <div className="max-w-40 space-y-1">
                <label htmlFor="gen-seed" className="text-sm font-medium">
                  Seed
                </label>
                <Input id="gen-seed" type="number" step={1} min={0} value={seed} onChange={(e) => setSeed(Number(e.target.value) || 0)} />
                {gen.deterministic && <p className="text-xs text-muted-foreground">Same seed and inputs give the same draft.</p>}
              </div>
              <Button onClick={() => void run()} disabled={busy}>
                <Sparkles /> {busy ? 'Generating…' : 'Generate'}
              </Button>
            </>
          )}
          {error && (
            <p role="alert" className="text-sm text-red-600 dark:text-red-400">
              {error.message}
              {error.fields.length > 0 && ` ${error.fields.map((f) => `${f.path}: ${f.message}`).join('; ')}`}
            </p>
          )}
          {result && (
            <div className="space-y-3 rounded-lg border p-3">
              <p className="text-sm">
                <strong>{result.draft.name}</strong>: {result.draft.n_sellers} sellers, {result.draft.horizon.min_rounds}–{result.draft.horizon.max_rounds} rounds, {result.draft.events.length} events.
              </p>
              <CheckList checks={result.check.checks} title="Check of the generated draft" />
              <div className="space-y-1">
                <p className="text-sm font-medium">{current ? `Changes to the current draft (${diff.length})` : `New draft (${diff.length} values)`}</p>
                <div className="max-h-60 overflow-auto rounded-md border">
                  <table className="w-full text-left font-mono text-xs">
                    <thead className="sticky top-0 bg-background">
                      <tr>
                        <th className="p-1.5">Field</th>
                        {current && <th className="p-1.5">Now</th>}
                        <th className="p-1.5">Generated</th>
                      </tr>
                    </thead>
                    <tbody>
                      {diff.map((c) => (
                        <tr key={c.path} className="border-t align-top">
                          <td className="p-1.5 text-muted-foreground">{c.path}</td>
                          {current && <td className="p-1.5 break-all">{show(c.before)}</td>}
                          <td className="p-1.5 break-all">{show(c.after)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            </div>
          )}
        </div>
        {result && (
          <DialogFooter>
            <Button variant="ghost" onClick={close}>
              Discard
            </Button>
            <Button variant="outline" onClick={() => use(false)}>
              Edit in the form
            </Button>
            <Button onClick={() => use(true)}>Accept and save</Button>
          </DialogFooter>
        )}
      </DialogContent>
    </Dialog>
  )
}
