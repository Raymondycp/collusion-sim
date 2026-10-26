import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Download, GitCompare, Save } from 'lucide-react'
import { savePreset, usePresets, type Preset, type RunConfig } from '@/api/runConfig'
import { isNotBuilt } from '@/api/client'
import { Button } from '@/components/ui/button'
import { InfoHint } from '@/components/InfoHint'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { diffConfigs } from '@/lib/jsonSchema'
import { NativeSelect, TextArea } from './SchemaField'

const show = (v: unknown) => (v === undefined ? '—' : JSON.stringify(v))

export function PresetDiff({ preset, draft }: { preset: Preset; draft: RunConfig }) {
  // preset_id records where the form came from; it is not a setting.
  const rows = diffConfigs({ ...preset.config, preset_id: undefined }, { ...draft, preset_id: undefined })
  return (
    <div className="space-y-1 rounded-lg border p-3">
      <p className="text-sm font-medium">
        {rows.length === 0 ? 'This form equals' : `${rows.length} field${rows.length === 1 ? '' : 's'} differ from`} {preset.name} v{preset.version}
      </p>
      {rows.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="text-left text-muted-foreground">
              <tr>
                <th className="py-1 pr-3 font-normal">Field</th>
                <th className="py-1 pr-3 font-normal">Preset</th>
                <th className="py-1 font-normal">This form</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {rows.map((r) => (
                <tr key={r.path} className="border-t align-top">
                  <td className="py-1 pr-3">{r.path}</td>
                  <td className="py-1 pr-3 break-all text-red-700 dark:text-red-400">{show(r.before)}</td>
                  <td className="py-1 break-all text-emerald-700 dark:text-emerald-400">{show(r.after)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function SaveDialog({ draft, onClose }: { draft: RunConfig; onClose: () => void }) {
  const qc = useQueryClient()
  const [name, setName] = useState(draft.name ?? '')
  const [notes, setNotes] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Save as preset</DialogTitle>
          <DialogDescription>A name that exists already gets a new version. A preset holds profile ids, never keys.</DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div className="space-y-1">
            <label htmlFor="preset-name" className="text-sm font-medium">
              Name
            </label>
            <Input id="preset-name" value={name} maxLength={120} onChange={(e) => setName(e.target.value)} autoComplete="off" />
          </div>
          <div className="space-y-1">
            <label htmlFor="preset-notes" className="text-sm font-medium">
              Notes
            </label>
            <TextArea id="preset-notes" value={notes} onChange={(e) => setNotes(e.target.value)} className="font-sans text-sm" />
          </div>
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
          <Button
            disabled={busy || !name.trim()}
            onClick={async () => {
              setBusy(true)
              try {
                await savePreset(name.trim(), draft, notes || undefined)
                await qc.invalidateQueries({ queryKey: ['presets'] })
                onClose()
              } catch (e) {
                setError(e instanceof Error ? e.message : 'Saving failed.')
              } finally {
                setBusy(false)
              }
            }}
          >
            Save
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export function PresetBar({ draft, onLoad }: { draft: RunConfig; onLoad: (p: Preset) => void }) {
  const presets = usePresets()
  const [selected, setSelected] = useState('')
  const [compare, setCompare] = useState(false)
  const [saving, setSaving] = useState(false)
  const preset = presets.data?.find((p) => p.id === selected)
  if (isNotBuilt(presets.error)) {
    return (
      <div className="flex items-center gap-1 text-xs text-muted-foreground">
        <span>Presets</span>
        <InfoHint label="About presets">
          Presets are not available on this server. This is normal: a preset is a saved setup, and this server has no preset store. Fill in the form by hand.
        </InfoHint>
      </div>
    )
  }
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-end gap-2">
        <div className="min-w-48 flex-1 space-y-1 sm:flex-none">
          <label htmlFor="preset-select" className="block text-sm font-medium">
            Preset
          </label>
          <NativeSelect id="preset-select" value={selected} onChange={(e) => setSelected(e.target.value)} className="sm:w-64">
            <option value="">{presets.isPending ? 'Loading…' : presets.data?.length ? 'Select a preset…' : 'No presets yet'}</option>
            {presets.data?.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name} v{p.version} ({p.config.kind})
              </option>
            ))}
          </NativeSelect>
        </div>
        <Button type="button" variant="outline" size="sm" disabled={!preset} onClick={() => preset && onLoad(preset)}>
          <Download /> Load
        </Button>
        <Button type="button" variant={compare ? 'secondary' : 'outline'} size="sm" disabled={!preset} aria-pressed={compare} onClick={() => setCompare((c) => !c)}>
          <GitCompare /> Compare
        </Button>
        <Button type="button" variant="outline" size="sm" onClick={() => setSaving(true)}>
          <Save /> Save as preset
        </Button>
      </div>
      {presets.error && <p className="text-sm text-red-600 dark:text-red-400">{presets.error.message}</p>}
      {compare && preset && <PresetDiff preset={preset} draft={draft} />}
      {saving && <SaveDialog draft={draft} onClose={() => setSaving(false)} />}
    </div>
  )
}
