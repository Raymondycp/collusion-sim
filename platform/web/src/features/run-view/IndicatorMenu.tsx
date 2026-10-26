import { useState } from 'react'
import { ChartLine, Plus, Settings2, X } from 'lucide-react'
import { defaultParams, paramFields, useIndicatorSpecs, type IndicatorParams, type IndicatorSelection, type IndicatorSpec, type ParamField } from '@/api/indicators'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'

function LayerBadge({ layer }: { layer: 'A' | 'B' }) {
  return (
    <span
      className="rounded border px-1 font-mono text-[10px]"
      title={layer === 'A' ? 'Layer A: reads the logs only' : 'Layer B: needs reference values (Nash, best response, shocks)'}
    >
      {layer}
    </span>
  )
}

function Field({ f, value, onChange }: { f: ParamField; value: IndicatorParams[string] | undefined; onChange: (v: IndicatorParams[string]) => void }) {
  const id = `param-${f.name}`
  if (f.type === 'boolean') {
    return (
      <label className="flex items-center gap-2 text-sm">
        <input id={id} type="checkbox" className="size-4" checked={Boolean(value ?? f.default)} onChange={(e) => onChange(e.target.checked)} />
        {f.title}
      </label>
    )
  }
  if (f.type === 'string' && f.enum) {
    return (
      <div className="space-y-1">
        <Label htmlFor={id}>{f.title}</Label>
        <Select value={String(value ?? f.default ?? '')} onValueChange={onChange}>
          <SelectTrigger id={id} className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {f.enum.map((o) => (
              <SelectItem key={o} value={o}>
                {o}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
    )
  }
  const numeric = f.type === 'integer' || f.type === 'number'
  return (
    <div className="space-y-1">
      <Label htmlFor={id}>
        {f.title}
        {numeric && (f.minimum !== undefined || f.maximum !== undefined) && (
          <span className="font-normal text-muted-foreground">
            {' '}
            ({f.minimum ?? '…'} to {f.maximum ?? '…'})
          </span>
        )}
      </Label>
      <Input
        id={id}
        type={numeric ? 'number' : 'text'}
        step={f.type === 'integer' ? 1 : 'any'}
        min={numeric ? f.minimum : undefined}
        max={numeric ? f.maximum : undefined}
        value={String(value ?? f.default ?? '')}
        onChange={(e) => onChange(numeric ? Number(e.target.value) : e.target.value)}
      />
    </div>
  )
}

function invalid(fields: ParamField[], params: IndicatorParams): string | null {
  for (const f of fields) {
    const v = params[f.name]
    if (f.type !== 'integer' && f.type !== 'number') continue
    if (typeof v !== 'number' || Number.isNaN(v)) return `${f.title}: enter a number.`
    if (f.type === 'integer' && !Number.isInteger(v)) return `${f.title}: enter a whole number.`
    if (f.minimum !== undefined && v < f.minimum) return `${f.title}: the minimum is ${f.minimum}.`
    if (f.maximum !== undefined && v > f.maximum) return `${f.title}: the maximum is ${f.maximum}.`
  }
  return null
}

function ParamsDialog({ spec, initial, onSave, onClose }: { spec: IndicatorSpec; initial: IndicatorParams; onSave: (p: IndicatorParams) => void; onClose: () => void }) {
  const fields = paramFields(spec)
  const [params, setParams] = useState<IndicatorParams>({ ...defaultParams(spec), ...initial })
  const error = invalid(fields, params)
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{spec.name}</DialogTitle>
          <DialogDescription>{spec.description}</DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          {fields.length === 0 && <p className="text-sm text-muted-foreground">This indicator has no settings.</p>}
          {fields.map((f) => (
            <Field key={f.name} f={f} value={params[f.name]} onChange={(v) => setParams((p) => ({ ...p, [f.name]: v }))} />
          ))}
          <p className="text-xs text-muted-foreground">
            Reads {spec.inputs.join(', ')}. Layer {spec.data_layer}. Drawn {spec.pane === 'overlay' ? 'on the price chart' : 'in its own pane'}. Version {spec.version}.
          </p>
          {error && (
            <p role="alert" className="text-sm text-red-600 dark:text-red-400">
              {error}
            </p>
          )}
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button disabled={error !== null} onClick={() => onSave(params)}>
            Apply
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

type Props = { selection: IndicatorSelection[]; upsert: (s: IndicatorSelection) => void; remove: (id: string) => void; error: string | null }

export function IndicatorMenu({ selection, upsert, remove, error }: Props) {
  const specs = useIndicatorSpecs()
  const [listOpen, setListOpen] = useState(false)
  const [editing, setEditing] = useState<{ spec: IndicatorSpec; params: IndicatorParams } | null>(null)
  const byId = new Map((specs.data ?? []).map((s) => [s.id, s]))

  return (
    <div className="flex flex-wrap items-center gap-2">
      <Button variant="outline" size="sm" onClick={() => setListOpen(true)}>
        <ChartLine /> Indicators
      </Button>
      {selection.map((s) => {
        const spec = byId.get(s.id)
        const args = Object.values(s.params).join(', ')
        return (
          <span key={s.id} className="inline-flex items-center gap-1 rounded-md border py-0.5 pr-0.5 pl-2 text-xs">
            {spec?.name ?? s.id}
            {args && <span className="text-muted-foreground">({args})</span>}
            {spec && (
              <Button variant="ghost" size="icon" className="size-6" aria-label={`Settings for ${spec.name}`} onClick={() => setEditing({ spec, params: s.params })}>
                <Settings2 />
              </Button>
            )}
            <Button variant="ghost" size="icon" className="size-6" aria-label={`Remove ${spec?.name ?? s.id}`} onClick={() => remove(s.id)}>
              <X />
            </Button>
          </span>
        )
      })}
      {error && (
        <span role="alert" className="text-xs text-red-600 dark:text-red-400">
          {error}
        </span>
      )}

      <Dialog open={listOpen} onOpenChange={setListOpen}>
        <DialogContent className="max-h-[85dvh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>Indicators</DialogTitle>
            <DialogDescription>The backend computes every indicator from the recorded run. A value at round t uses only rounds up to t.</DialogDescription>
          </DialogHeader>
          {specs.isPending && <Skeleton className="h-32 w-full" />}
          {specs.isError && <p className="text-sm text-red-600">{specs.error.message}</p>}
          <ul className="divide-y rounded-lg border">
            {specs.data?.map((spec) => {
              const added = selection.some((s) => s.id === spec.id)
              return (
                <li key={spec.id} className="flex items-start gap-3 p-3">
                  <div className="min-w-0 flex-1 space-y-0.5">
                    <div className="flex flex-wrap items-center gap-2 text-sm font-medium">
                      {spec.name} <LayerBadge layer={spec.data_layer} />
                      <span className="text-xs font-normal text-muted-foreground">{spec.pane === 'overlay' ? 'on price chart' : 'own pane'}</span>
                    </div>
                    <p className="text-xs text-muted-foreground">{spec.description}</p>
                  </div>
                  <Button
                    size="sm"
                    variant={added ? 'secondary' : 'outline'}
                    onClick={() => {
                      setListOpen(false)
                      setEditing({ spec, params: selection.find((s) => s.id === spec.id)?.params ?? defaultParams(spec) })
                    }}
                  >
                    {added ? <Settings2 /> : <Plus />}
                    {added ? 'Edit' : 'Add'}
                  </Button>
                </li>
              )
            })}
          </ul>
        </DialogContent>
      </Dialog>

      {editing && (
        <ParamsDialog
          spec={editing.spec}
          initial={editing.params}
          onClose={() => setEditing(null)}
          onSave={(params) => {
            upsert({ id: editing.spec.id, params })
            setEditing(null)
          }}
        />
      )}
    </div>
  )
}
