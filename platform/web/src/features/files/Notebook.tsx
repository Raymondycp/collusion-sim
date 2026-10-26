import { MarkdownView } from './Markdown'

type Output = {
  output_type?: string
  name?: string
  text?: string | string[]
  data?: Record<string, string | string[]>
  ename?: string
  evalue?: string
}
type Cell = { cell_type?: string; source?: string | string[]; outputs?: Output[]; execution_count?: number | null }

/** A notebook stores text as a string or as a list of lines. */
const join = (v: string | string[] | undefined) => (Array.isArray(v) ? v.join('') : (v ?? ''))

const SHOWN = ['image/png', 'image/jpeg', 'text/plain']

function OutputView({ o }: { o: Output }) {
  if (o.output_type === 'stream') return <pre className="overflow-x-auto px-3 py-1 text-xs whitespace-pre-wrap">{join(o.text)}</pre>
  if (o.output_type === 'error')
    return <pre className="overflow-x-auto px-3 py-1 text-xs whitespace-pre-wrap text-red-600">{`${o.ename ?? 'Error'}: ${o.evalue ?? ''}`}</pre>
  const data = o.data ?? {}
  const img = data['image/png'] ? 'image/png' : data['image/jpeg'] ? 'image/jpeg' : null
  const ignored = Object.keys(data).filter((k) => !SHOWN.includes(k))
  return (
    <div className="px-3 py-1">
      {img && <img alt="Notebook output" className="max-w-full" src={`data:${img};base64,${join(data[img]).replace(/\s/g, '')}`} />}
      {!img && data['text/plain'] && <pre className="overflow-x-auto text-xs whitespace-pre-wrap">{join(data['text/plain'])}</pre>}
      {ignored.length > 0 && <p className="text-xs text-muted-foreground">Output of type {ignored.join(', ')} is not shown.</p>}
    </div>
  )
}

export function NotebookView({ text }: { text: string }) {
  let cells: Cell[]
  try {
    const nb = JSON.parse(text) as { cells?: Cell[] }
    cells = Array.isArray(nb.cells) ? nb.cells : []
  } catch {
    return <p className="p-3 text-sm text-red-600">This file is not a valid notebook (JSON could not be read).</p>
  }
  if (cells.length === 0) return <p className="p-3 text-sm text-muted-foreground">The notebook has no cells.</p>
  return (
    <div className="space-y-3 p-3" data-testid="notebook">
      {cells.map((c, i) =>
        c.cell_type === 'markdown' ? (
          <MarkdownView key={i} text={join(c.source)} />
        ) : (
          <div key={i} className="rounded border">
            <pre className="overflow-x-auto bg-muted/50 px-3 py-2 text-xs">
              <code>{join(c.source)}</code>
            </pre>
            {(c.outputs ?? []).map((o, j) => (
              <OutputView key={j} o={o} />
            ))}
          </div>
        ),
      )}
    </div>
  )
}
