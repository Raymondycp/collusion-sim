import { useState } from 'react'
import { Link } from 'react-router'
import { Plus } from 'lucide-react'
import { useScenarioList, type ScenarioStatus } from '@/api/scenarios'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'

const FILTERS: { value: ScenarioStatus | 'all'; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'frozen', label: 'Frozen' },
  { value: 'draft', label: 'Drafts' },
]

export function StatusBadge({ status }: { status: ScenarioStatus }) {
  return (
    <span className={status === 'frozen' ? 'rounded border px-1.5 text-xs' : 'rounded border border-dashed px-1.5 text-xs text-muted-foreground'}>{status}</span>
  )
}

export function ScenariosPage() {
  const [filter, setFilter] = useState<ScenarioStatus | 'all'>('all')
  const list = useScenarioList(filter === 'all' ? undefined : filter)
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-lg font-semibold">Scenarios</h1>
        <ToggleGroup type="single" variant="outline" size="sm" value={filter} onValueChange={(v) => v && setFilter(v as typeof filter)} aria-label="Status">
          {FILTERS.map((f) => (
            <ToggleGroupItem key={f.value} value={f.value}>
              {f.label}
            </ToggleGroupItem>
          ))}
        </ToggleGroup>
        <Button asChild size="sm" className="ml-auto">
          <Link to="/scenarios/new">
            <Plus /> New draft
          </Link>
        </Button>
      </div>
      {list.isPending && <Skeleton className="h-32 w-full" />}
      {list.error && (
        <p role="alert" className="text-sm text-red-600 dark:text-red-400">
          {list.error.message}
        </p>
      )}
      {list.data && list.data.length === 0 && <p className="text-sm text-muted-foreground">No scenarios. Start one with New draft.</p>}
      {list.data && list.data.length > 0 && (
        <div className="overflow-x-auto rounded-lg border">
          <table className="w-full text-left text-sm">
            <thead className="bg-muted/50 text-xs text-muted-foreground">
              <tr>
                <th className="p-2">Scenario</th>
                <th className="p-2">Status</th>
                <th className="p-2">Sellers</th>
                <th className="p-2">Events</th>
                <th className="p-2">Hash</th>
                <th className="p-2">Updated</th>
              </tr>
            </thead>
            <tbody>
              {list.data.map((s) => (
                <tr key={s.id} className="border-t">
                  <td className="p-2">
                    <Link to={`/scenarios/${s.id}`} className="font-medium underline-offset-2 hover:underline">
                      {s.name || s.id}
                    </Link>
                    <div className="font-mono text-xs text-muted-foreground">{s.id}</div>
                  </td>
                  <td className="p-2">
                    <StatusBadge status={s.status} />
                  </td>
                  <td className="p-2">{s.n_sellers}</td>
                  <td className="p-2">{s.n_events}</td>
                  <td className="max-w-40 truncate p-2 font-mono text-xs" title={s.hash ?? undefined}>
                    {s.hash ?? 'none (draft)'}
                  </td>
                  <td className="p-2 text-xs whitespace-nowrap">{new Date(s.updated_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
