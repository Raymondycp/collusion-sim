import { Fragment, useState } from 'react'
import { Download } from 'lucide-react'
import { asApiError } from '@/api/client'
import { saveExport, type ExportGroup, type ExportItem } from '@/api/exports'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { DropdownMenu, DropdownMenuCheckboxItem, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'

const groupTitle = (g: ExportGroup | 'extra', live: boolean) => {
  const sofar = live ? ' so far' : ''
  return {
    record: `Full record${sofar} (includes private channels)`,
    prices: `Market data${sofar}`,
    p2: `Feature table as JSON${sofar}`,
    sheet: `Feature table as CSV${sofar}`,
    extra: 'Picture',
  }[g]
}

/** One line under a group title, for what the group is for. */
const groupAbout: Partial<Record<ExportGroup | 'extra', string>> = {
  p2: 'The P2 starter reads this file.',
  sheet: 'Opens in a spreadsheet. The P2 starter also reads the pair.',
}

/** True today: the web has no judge job screen (FLOWS-web F14). Change it here the day the screen exists (CR-92 7). */
const JUDGE_JOBS_FROM = 'Judge jobs run from the CLI (see Evaluate).'

type Extra = { label: string; about?: string; onSelect: () => void }

/** Check boxes above the items, for an option the items take (the judge jobs of an experiment). The menu stays open on a click. */
type Choice = { title: string; options: { id: string; label: string }[]; selected: string[]; onChange: (ids: string[]) => void }

/** Export button for a run or an experiment. `extra` adds items that do not come from the API (the chart PNG). `live`: the run or the experiment has not ended. */
export function ExportMenu({ id, items, extra = [], live = false, choice, publicView = false }: { id: string; items: ExportItem[]; extra?: Extra[]; live?: boolean; choice?: Choice; publicView?: boolean }) {
  const [busy, setBusy] = useState<string | null>(null)
  const [confirming, setConfirming] = useState<ExportItem | null>(null)
  const [error, setError] = useState<string | null>(null)
  const save = async (item: ExportItem) => {
    setBusy(item.label)
    setError(null)
    try {
      await saveExport(item, id)
    } catch (e) {
      setError(`${item.label}: ${asApiError(e).message}`)
    } finally {
      setBusy(null)
    }
  }
  const groups = [...new Set(items.map((x) => x.group))].map((g) => ({
    g,
    // In the public view, an item with private files asks first (UX review 3, item 2). The market-data CSV does not.
    rows: items.filter((x) => x.group === g).map((x) => ({ label: x.label, about: x.about, onSelect: () => (publicView && x.private ? setConfirming(x) : void save(x)) })),
  }))
  const all: { g: ExportGroup | 'extra'; rows: Extra[] }[] = extra.length ? [...groups, { g: 'extra', rows: extra }] : groups
  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button variant="outline" size="sm" disabled={busy !== null}>
            <Download /> {busy ? 'Exporting…' : 'Export'}
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-auto max-w-[min(32rem,90vw)]">
          {choice && (
            <>
              <DropdownMenuLabel className="text-xs font-normal text-muted-foreground">{choice.title}</DropdownMenuLabel>
              {choice.options.length === 0 && (
                <p className="px-2 pb-1 text-xs text-muted-foreground">
                  No completed judge job for this experiment. The tables then have no judge columns. {JUDGE_JOBS_FROM}
                </p>
              )}
              {choice.options.map((o) => (
                <DropdownMenuCheckboxItem
                  key={o.id}
                  checked={choice.selected.includes(o.id)}
                  onSelect={(e) => e.preventDefault()}
                  onCheckedChange={(on) => choice.onChange(on ? [...choice.selected, o.id] : choice.selected.filter((x) => x !== o.id))}>
                  {o.label}
                </DropdownMenuCheckboxItem>
              ))}
              <DropdownMenuSeparator />
            </>
          )}
          {all.map(({ g, rows }, i) => (
            <Fragment key={g}>
              {i > 0 && <DropdownMenuSeparator />}
              <DropdownMenuLabel className="text-xs font-normal text-muted-foreground">
                {groupTitle(g, live)}
                {groupAbout[g] && <span className="block">{groupAbout[g]}</span>}
              </DropdownMenuLabel>
              {rows.map((x, j) => (
                <DropdownMenuItem key={x.label} aria-label={x.label} aria-describedby={x.about ? `export-about-${g}-${j}` : undefined} onSelect={x.onSelect} className="flex-col items-start gap-0">
                  <span>{x.label}</span>
                  {x.about && (
                    <span id={`export-about-${g}-${j}`} className="max-w-[28rem] text-xs font-normal text-muted-foreground">
                      {x.about}
                    </span>
                  )}
                </DropdownMenuItem>
              ))}
            </Fragment>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>
      {publicView && (
        <span role="note" className="text-xs text-amber-700 dark:text-amber-400">
          Public view does not change exports: the full record still contains private data.
        </span>
      )}
      {confirming && (
        <Dialog open onOpenChange={(o) => !o && setConfirming(null)}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Export private data?</DialogTitle>
              <DialogDescription>
                You are in Public view, but &quot;{confirming.label}&quot; holds private data. The export does not hide it. It contains:
              </DialogDescription>
            </DialogHeader>
            <ul aria-label="Private files" className="list-disc space-y-1 pl-5 text-sm">
              {confirming.private?.map((f) => (
                <li key={f}>{f}</li>
              ))}
            </ul>
            <DialogFooter>
              <Button variant="outline" onClick={() => setConfirming(null)}>
                Cancel
              </Button>
              <Button
                onClick={() => {
                  const item = confirming
                  setConfirming(null)
                  void save(item)
                }}>
                Export with private data
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
      {error && (
        <span role="alert" className="text-xs text-red-600 dark:text-red-400">
          {error}
        </span>
      )}
    </span>
  )
}
