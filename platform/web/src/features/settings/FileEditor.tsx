import { useState, type ChangeEvent } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Upload } from 'lucide-react'
import type { StoredFile } from '@/api/settings'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { ApiError } from '@/api/client'
import { TextArea } from '@/features/new-run/SchemaField'
import { ErrorText } from './SettingsLayout'

const MAX_BYTES = 256 * 1024

/** A text file that the server checks, stores and hashes. The server is the only judge of the content; this editor only sends the text. */
export function FileEditor({ id, file, queryKey, save, readOnly, alsoChanges = [] }: { id: string; file: StoredFile; queryKey: readonly unknown[]; save: (text: string) => Promise<StoredFile>; readOnly?: string; alsoChanges?: (readonly unknown[])[] }) {
  const qc = useQueryClient()
  const [text, setText] = useState(file.text ?? '')
  const [uploadError, setUploadError] = useState<string | null>(null)
  const put = useMutation({
    mutationFn: () => save(text),
    onSuccess: async (f) => {
      qc.setQueryData(queryKey, f)
      await Promise.all(alsoChanges.map((k) => qc.invalidateQueries({ queryKey: k })))
    },
  })
  const dirty = text !== (file.text ?? '')

  async function upload(e: ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0]
    e.target.value = ''
    if (!f) return
    if (f.size > MAX_BYTES) return setUploadError(`${f.name} is larger than 256 KB.`)
    setUploadError(null)
    setText(await f.text())
  }

  return (
    <div className="space-y-2">
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
        <dt className="text-muted-foreground">File</dt>
        <dd className="font-mono text-xs break-all">{file.name}</dd>
        <dt className="text-muted-foreground">Hash</dt>
        <dd className="font-mono text-xs break-all">{file.hash ?? 'none: the file does not exist yet'}</dd>
        <dt className="text-muted-foreground">Saved</dt>
        <dd>{file.updated_at ? new Date(file.updated_at).toLocaleString() : 'never'}</dd>
      </dl>
      {readOnly && <p className="text-sm text-muted-foreground">{readOnly}</p>}
      <Label htmlFor={`${id}-text`}>Text</Label>
      <TextArea
        id={`${id}-text`}
        className="min-h-64"
        spellCheck={false}
        value={text}
        readOnly={!!readOnly}
        onChange={(e) => setText(e.target.value)}
      />
      <ErrorText>{uploadError ?? put.error?.message}</ErrorText>
      {put.error instanceof ApiError && put.error.fields.length > 0 && (
        <ul aria-label="Problems in the file" className="ml-4 list-disc text-sm text-red-700 dark:text-red-400">
          {put.error.fields.map((f) => (
            <li key={`${f.path}:${f.message}`}>
              {f.path}: {f.message}
            </li>
          ))}
        </ul>
      )}
      {!readOnly && (
        <div className="flex flex-wrap items-center gap-2">
          <Button type="button" disabled={!dirty || put.isPending} onClick={() => put.mutate()}>
            {put.isPending ? 'Saving…' : 'Save'}
          </Button>
          <Button type="button" variant="outline" asChild>
            <label htmlFor={`${id}-upload`} className="cursor-pointer">
              <Upload /> Load a file
            </label>
          </Button>
          <input id={`${id}-upload`} type="file" accept=".yaml,.yml,.txt,text/plain" className="sr-only" onChange={upload} />
          {dirty && <span className="text-sm text-muted-foreground">Not saved.</span>}
          {put.isSuccess && !dirty && <span role="status" className="text-sm text-muted-foreground">Saved.</span>}
        </div>
      )}
    </div>
  )
}
