import { Link, useParams } from 'react-router'
import { ArrowLeft } from 'lucide-react'
import { useConditions } from '@/api/runConfig'
import { conditionFileKey, FREEZE_KEY, saveConditionFile, useConditionFile } from '@/api/settings'
import { Skeleton } from '@/components/ui/skeleton'
import { ConditionStatus } from '@/features/conditions/ConditionsPage'
import { FileEditor } from './FileEditor'
import { ErrorText, PageHead } from './SettingsLayout'

export function ConditionsSettingsPage() {
  const conditions = useConditions()
  return (
    <div className="max-w-3xl space-y-4">
      <PageHead title="Condition files">A draft condition can change here. A frozen one is read only. Each save writes the YAML file, so git shows the diff.</PageHead>
      <ErrorText>{conditions.error?.message}</ErrorText>
      {conditions.isPending && <Skeleton className="h-40 w-full" />}
      <ul className="divide-y rounded-lg border">
        {conditions.data?.map((c) => (
          <li key={c.id} className="flex flex-wrap items-center gap-2 p-3 text-sm">
            <Link to={c.id} className="font-mono font-medium underline-offset-2 hover:underline">
              {c.id}
            </Link>
            <span className="min-w-0 flex-1 text-muted-foreground">{c.name}</span>
            <ConditionStatus c={c} />
            <Link to={c.id} className="text-sm underline-offset-2 hover:underline">
              {c.status === 'draft' ? 'Edit' : 'View'}
            </Link>
          </li>
        ))}
      </ul>
    </div>
  )
}

export function ConditionFilePage() {
  const { conditionId = '' } = useParams()
  const conditions = useConditions()
  const c = conditions.data?.find((x) => x.id === conditionId)
  const file = useConditionFile(conditionId)
  return (
    <div className="max-w-3xl space-y-4">
      <Link to=".." relative="path" className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeft className="size-4" /> Condition files
      </Link>
      <PageHead title={`Condition ${conditionId}`}>
        {c ? `${c.name}. ` : ''}Labels, prompts, sellers, channels and limits are in this file. A run takes its labels from here; no run form can set them.
      </PageHead>
      {c && <ConditionStatus c={c} />}
      <ErrorText>{file.error?.message ?? conditions.error?.message}</ErrorText>
      {(file.isPending || conditions.isPending) && <Skeleton className="h-64 w-full" />}
      {file.data && c && (
        <FileEditor
          id={`cond-${conditionId}`}
          file={file.data}
          queryKey={conditionFileKey(conditionId)}
          save={(text) => saveConditionFile(conditionId, text)}
          alsoChanges={[['conditions'], ['condition', conditionId], FREEZE_KEY]}
          readOnly={c.status === 'frozen' ? 'This condition is frozen. It cannot change.' : undefined}
        />
      )}
    </div>
  )
}
