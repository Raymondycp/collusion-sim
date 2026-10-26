import { useState } from 'react'
import { FREEZE_KEY, RESEARCH_FILES, researchFileKey, saveResearchFile, useFreezePreview, useResearchFile, type ResearchFile } from '@/api/settings'
import { Skeleton } from '@/components/ui/skeleton'
import { FileEditor } from './FileEditor'
import { ErrorText, PageHead } from './SettingsLayout'

function ResearchFileSection({ id, title, about, frozenBy, note }: { id: ResearchFile; title: string; about: string; frozenBy: string | null; note: string | null }) {
  const f = useResearchFile(id)
  const [invalidated, setInvalidated] = useState(false)
  const save = async (text: string) => {
    const saved = await saveResearchFile(id, text)
    setInvalidated(saved.invalidated_stage === 'rule')
    return saved
  }
  return (
    <section aria-label={title} className="space-y-2 rounded-lg border p-3">
      <h2 className="font-medium">{title}</h2>
      <p className="text-sm text-muted-foreground">{about}</p>
      <ErrorText>{f.error?.message}</ErrorText>
      {f.isPending && <Skeleton className="h-40 w-full" />}
      {note && !frozenBy && <p className="rounded-md border border-amber-500/40 p-2 text-sm">{note}</p>}
      {invalidated && (
        <p role="alert" className="text-sm text-red-700 dark:text-red-400">
          This save made stage 2 not valid. The test seeds are locked until you freeze stage 2 again.
        </p>
      )}
      {f.data && <FileEditor id={id} file={f.data} queryKey={researchFileKey(id)} save={save} alsoChanges={[FREEZE_KEY]} readOnly={frozenBy ? `Freeze ${frozenBy} holds this file. It cannot change now.` : undefined} />}
    </section>
  )
}

/** The judge rubric is read only once stage 1 is frozen (409 research_frozen). The decision rule stays editable until a test cell is used; the server refuses after that. */
export function ResearchFilesPage() {
  const freeze = useFreezePreview()
  const stages = freeze.data?.stages
  return (
    <div className="max-w-3xl space-y-4">
      <PageHead title="Research files">Freeze stage 1 needs the judge rubric; stage 2 needs the decision rule. The server checks the YAML when you save, and git shows each change.</PageHead>
      {RESEARCH_FILES.map((r) => (
        <ResearchFileSection key={r.id} id={r.id} title={r.title} about={`${r.about} Stored as ${r.file}.`} frozenBy={r.stage === 'conditions' && stages?.conditions.frozen ? 'stage 1' : null}
          note={r.stage === 'rule' && stages?.rule.frozen ? 'Stage 2 is frozen. A save here makes it not valid and locks the test seeds until you freeze stage 2 again.' : null}
        />
      ))}
    </div>
  )
}
