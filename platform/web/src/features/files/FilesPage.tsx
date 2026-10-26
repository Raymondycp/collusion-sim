import { lazy, Suspense, useRef, useState } from 'react'
import { Code, Eye, X } from 'lucide-react'
import { extOf, fileUrl, IMAGE_EXT, sourceKey, useFileText, type FileSource } from '@/api/files'
import { Button } from '@/components/ui/button'
import { useTheme } from '@/app/theme'
import { cn } from '@/lib/utils'
import { Explorer } from './Explorer'
import { MarkdownView } from './Markdown'
import { NotebookView } from './Notebook'

// Monaco is its own chunk: it loads when the first text file opens.
const MonacoView = lazy(() => import('./MonacoView'))

type Tab = { key: string; title: string; source: FileSource; preview: boolean }

const LANGUAGE: Record<string, string> = {
  json: 'json',
  jsonl: 'json',
  ipynb: 'json',
  md: 'markdown',
  py: 'python',
  yaml: 'yaml',
  yml: 'yaml',
  html: 'html',
  htm: 'html',
}
const PREVIEW_EXT = ['md', 'ipynb', 'html', 'htm']

/** The extension decides the language and the preview: a run file is JSON, a zip member has its own name. */
function nameOf(s: FileSource): string {
  return s.kind === 'repo' ? s.path : s.kind === 'zip' ? s.member : s.kind === 'run-meta' ? 'meta.json' : 'episode.jsonl'
}

function TabBody({ tab }: { tab: Tab }) {
  const dark = useTheme() === 'dark'
  const ext = extOf(nameOf(tab.source))
  const isImage = tab.source.kind === 'repo' && IMAGE_EXT.includes(ext)
  const q = useFileText(tab.source, !isImage)
  if (isImage && tab.source.kind === 'repo')
    return (
      <div className="h-full overflow-auto p-3">
        <img src={fileUrl(tab.source.path)} alt={tab.title} className="max-w-full" />
      </div>
    )
  if (q.isPending) return <p className="p-3 text-sm text-muted-foreground">Loading...</p>
  if (q.isError) return <p className="p-3 text-sm text-red-600">{q.error.message}</p>
  const { text, truncated } = q.data
  const showPreview = tab.preview && PREVIEW_EXT.includes(ext)
  return (
    <div className="flex h-full flex-col">
      {truncated && <p className="border-b bg-amber-100 px-3 py-1 text-xs text-amber-900">This file is large. Only the first 5 MB is shown.</p>}
      <div className="min-h-0 flex-1">
        {showPreview && ext === 'md' ? (
          <div className="h-full overflow-auto p-4"><MarkdownView text={text} dir={tab.source.kind === 'repo' ? tab.source.path.split('/').slice(0, -1).join('/') : undefined} /></div>
        ) : showPreview && ext === 'ipynb' ? (
          <div className="h-full overflow-auto"><NotebookView text={text} /></div>
        ) : showPreview ? (
          <iframe title={`Preview of ${tab.title}`} sandbox="allow-scripts" srcDoc={text} className="h-full w-full bg-white" />
        ) : (
          <Suspense fallback={<p className="p-3 text-sm text-muted-foreground">Loading editor...</p>}>
            <MonacoView path={tab.key} value={text} language={LANGUAGE[ext] ?? 'plaintext'} dark={dark} />
          </Suspense>
        )}
      </div>
    </div>
  )
}

export function FilesPage() {
  const [tabs, setTabs] = useState<Tab[]>([])
  const [active, setActive] = useState<string | null>(null)
  const [width, setWidth] = useState(280)
  const drag = useRef<{ x: number; w: number } | null>(null)

  const open = (source: FileSource, title: string) => {
    const key = sourceKey(source)
    setTabs((t) => (t.some((x) => x.key === key) ? t : [...t, { key, title, source, preview: false }]))
    setActive(key)
  }
  const close = (key: string) => {
    const i = tabs.findIndex((t) => t.key === key)
    const rest = tabs.filter((t) => t.key !== key)
    setTabs(rest)
    if (active === key) setActive(rest[Math.min(i, rest.length - 1)]?.key ?? null)
  }
  const togglePreview = (key: string) => setTabs((t) => t.map((x) => (x.key === key ? { ...x, preview: !x.preview } : x)))
  const current = tabs.find((t) => t.key === active)
  const canPreview = current && PREVIEW_EXT.includes(extOf(nameOf(current.source))) && !(current.source.kind === 'repo' && IMAGE_EXT.includes(extOf(nameOf(current.source))))

  return (
    <div className="flex h-[calc(100dvh-6.5rem)] min-h-[420px] overflow-hidden rounded-md border" data-testid="files-page">
      <aside aria-label="Library" className="shrink-0 border-r" style={{ width }}>
        <Explorer open={open} activeKey={active} />
      </aside>
      <div
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize the library"
        className="w-1 shrink-0 cursor-col-resize hover:bg-primary/30"
        onPointerDown={(e) => {
          e.currentTarget.setPointerCapture(e.pointerId)
          drag.current = { x: e.clientX, w: width }
        }}
        onPointerMove={(e) => {
          if (drag.current) setWidth(Math.min(600, Math.max(180, drag.current.w + e.clientX - drag.current.x)))
        }}
        onPointerUp={() => (drag.current = null)}
      />
      <section aria-label="Editor" className="flex min-w-0 flex-1 flex-col">
        <div className="flex items-stretch border-b bg-muted/40">
          <div role="tablist" className="flex min-w-0 flex-1 overflow-x-auto">
            {tabs.map((t) => (
              <div key={t.key} className={cn('flex shrink-0 items-center border-r text-[13px]', t.key === active ? 'bg-background' : 'text-muted-foreground')}>
                <button type="button" role="tab" aria-selected={t.key === active} title={nameOf(t.source)} onClick={() => setActive(t.key)} className="max-w-56 truncate py-1.5 pr-1 pl-3">
                  {t.title}
                </button>
                <button type="button" aria-label={`Close ${t.title}`} onClick={() => close(t.key)} className="mr-1 rounded p-0.5 hover:bg-muted">
                  <X className="size-3.5" />
                </button>
              </div>
            ))}
          </div>
          {canPreview && (
            <Button variant="ghost" size="sm" className="h-auto rounded-none" aria-pressed={current.preview} onClick={() => togglePreview(current.key)}>
              {current.preview ? <Code /> : <Eye />}
              {current.preview ? 'Source' : 'Preview'}
            </Button>
          )}
        </div>
        <div className="min-h-0 flex-1" role="tabpanel">
          {current ? <TabBody key={current.key} tab={current} /> : <p className="p-4 text-sm text-muted-foreground">Open a file from the library.</p>}
        </div>
      </section>
    </div>
  )
}
