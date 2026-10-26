import { useState, type KeyboardEvent, type ReactNode } from 'react'
import { ChevronLeft, ChevronRight, Copy } from 'lucide-react'
import { asApiError } from '@/api/client'
import { useCallTrail, type TrailLine, type TrailPart, type TrailView } from '@/api/trail'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import { PURPOSE_TEXT } from './labels'
import { usdCall } from '@/lib/format'

const MISSING_TEXT: Record<NonNullable<TrailLine['missing']>, string> = {
  no_cache_row: 'No request or answer is stored: the call failed before a reply.',
  reply: 'The call failed: the request is stored, but there is no answer.',
  unreadable_row: 'The stored row of this call cannot be read. The other calls are not affected.',
}

/** The line's own token fields: null is "unknown". The counts inside `messages` are 0 when unknown, so the panel does not read them. */
const count = (n: number | null | undefined) => (n == null ? 'unknown' : String(n))

function tokens(l: TrailLine): string {
  const input = `${count(l.input_tokens)} in${l.cached_input_tokens ? ` (${l.cached_input_tokens} cached)` : ''}`
  const output = `${count(l.output_tokens)} out${l.reasoning_tokens ? ` (${l.reasoning_tokens} reasoning)` : ''}`
  return `${input}, ${output}`
}

/** The rows of the call head. `plain` gives text only, for "Copy metadata". */
function headRows(l: TrailLine, plain = false): [string, ReactNode][] {
  const rows: [string, ReactNode][] = [
    ['Call', `${l.agent_id}, ${PURPOSE_TEXT[l.purpose]}, R${l.t}, attempt ${l.attempt}`],
    ['Model', `${l.model}${l.sample_index ? `, sample ${l.sample_index}` : ''}`],
    ['Cache', l.cache_hit ? `hit${l.paid_by_run && l.paid_by_run !== l.run_id ? `, paid by ${l.paid_by_run}` : ''}` : 'no hit'],
    [
      'Finish',
      l.finish_reason === 'length' ? (
        plain ? 'length: answer cut at the token limit' : <span className="text-amber-700 dark:text-amber-400">length: answer cut at the token limit</span>
      ) : (
        (l.finish_reason ?? 'not recorded')
      ),
    ],
    ['Tokens', tokens(l)],
    ['Cost', usdCall(l.cost_usd)],
    ['Latency', l.latency_ms == null ? 'unknown' : `${l.latency_ms} ms`],
  ]
  if (l.settings) rows.push(['Settings', Object.entries(l.settings).map(([k, v]) => `${k} ${typeof v === 'object' && v !== null ? JSON.stringify(v) : String(v)}`).join(', ')])
  if (l.error) rows.push(['Error', [l.error, l.http_status != null ? `HTTP ${l.http_status}` : null, l.provider_code].filter(Boolean).join(', ')])
  return rows
}

const metaText = (l: TrailLine) => headRows(l, true).map(([k, v]) => `${k}: ${String(v)}`).join('\n')

function Head({ line: l }: { line: TrailLine }) {
  const rows = headRows(l)
  return (
    <dl aria-label="Call" className="grid grid-cols-[6rem_minmax(0,1fr)] gap-x-3 gap-y-0.5 text-xs">
      {rows.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-muted-foreground">{k}</dt>
          <dd className="break-words">{v}</dd>
        </div>
      ))}
    </dl>
  )
}

function Block({ title, text, open = true }: { title: string; text: string; open?: boolean }) {
  return (
    <details open={open} className="rounded-md border text-xs">
      <summary className="flex cursor-pointer items-center justify-between gap-2 px-2 py-1 font-medium">
        {title}
        <Button type="button" variant="ghost" size="icon-sm" aria-label={`Copy ${title}`} onClick={(e) => (e.preventDefault(), void navigator.clipboard?.writeText(text))}>
          <Copy />
        </Button>
      </summary>
      <pre className="max-h-80 overflow-auto border-t px-2 py-1 font-mono whitespace-pre-wrap break-words">{text}</pre>
    </details>
  )
}

/** The system prompt is long and the same in each call, and thinking is optional reading, so both start folded. */
function Part({ p }: { p: TrailPart }) {
  switch (p.kind) {
    case 'system':
      return <Block title="System prompt" text={p.text} open={false} />
    case 'user':
      return <Block title="User prompt" text={p.text} />
    case 'thinking':
      return <Block title="Thinking" text={p.text} open={false} />
    case 'text':
      return <Block title="Text" text={p.text} />
    case 'tool-return':
      return <Block title={`Tool result: ${p.tool}`} text={p.text} />
    case 'tool-call':
      return <Block title={`Tool call: ${p.tool}`} text={p.text} />
    case 'unknown':
      return <Block title={`Unknown part: ${p.partKind}`} text={p.text} />
  }
}

const ROLE_TEXT = { request: 'Request', answer: 'Answer', unknown: 'Unknown message' } as const

function Body({ q }: { q: ReturnType<typeof useCallTrail> }) {
  const [toolsOpen, setToolsOpen] = useState(false)
  if (q.isPending) return <p className="text-xs text-muted-foreground">Loading the trail…</p>
  if (q.isError) return <p role="alert" className="text-xs text-destructive">{asApiError(q.error).message}</p>
  const { line, tools, messages } = q.data
  return (
    <div className="space-y-3">
      <Head line={line} />
      {line.missing && <p className="rounded-md border border-amber-500/50 px-2 py-1 text-xs">{MISSING_TEXT[line.missing]}</p>}
      {tools && (
        <section aria-label="Tools offered" className="space-y-1">
          <h4 className="text-xs font-medium">Tools offered ({tools.length})</h4>
          <ul className="space-y-0.5 text-xs">
            {tools.map((t) => (
              <li key={t.name}>
                <span className="font-mono">{t.name}</span>
                {t.description && <span className="text-muted-foreground">: {t.description}</span>}
              </li>
            ))}
          </ul>
          <details open={toolsOpen} onToggle={(e) => setToolsOpen(e.currentTarget.open)} className="text-xs">
            <summary className="cursor-pointer text-muted-foreground">Parameter schemas</summary>
            {toolsOpen && tools.map((t) => <Block key={t.name} title={`${t.name} parameters`} text={t.schema} />)}
          </details>
        </section>
      )}
      {messages?.map((m, i) => (
        <section key={i} aria-label={ROLE_TEXT[m.role]} className="space-y-1">
          <h4 className="text-xs font-medium">{ROLE_TEXT[m.role]}</h4>
          {m.parts.length === 0 ? <p className="text-xs text-muted-foreground">No parts.</p> : m.parts.map((p, j) => <Part key={j} p={p} />)}
        </section>
      ))}
    </div>
  )
}

/** Copy buttons and the move to the call before or after (UX review item 3). The arrow keys move too. */
function Toolbar({ view, seq, seqs, onSelect }: { view: TrailView | undefined; seq: number; seqs: number[]; onSelect: (seq: number) => void }) {
  const [copied, setCopied] = useState<string | null>(null)
  const at = seqs.indexOf(seq)
  const copy = (what: string, text: string) => {
    // "Copied" only after the browser accepted the text: a page outside a secure context has no clipboard (CR-92 4).
    void navigator.clipboard?.writeText(text).then(() => {
      setCopied(what)
      setTimeout(() => setCopied((c) => (c === what ? null : c)), 1500)
    }, () => undefined)
  }
  return (
    <div role="toolbar" aria-label="Call actions" className="flex flex-wrap items-center gap-1.5">
      <Button type="button" variant="outline" size="sm" disabled={at <= 0} onClick={() => onSelect(seqs[at - 1]!)}>
        <ChevronLeft /> Previous call
      </Button>
      <Button type="button" variant="outline" size="sm" disabled={at === -1 || at >= seqs.length - 1} onClick={() => onSelect(seqs[at + 1]!)}>
        Next call <ChevronRight />
      </Button>
      {at !== -1 && (
        <span className="text-xs text-muted-foreground tabular-nums">
          {at + 1} of {seqs.length}
        </span>
      )}
      <Button type="button" variant="ghost" size="sm" disabled={!view} onClick={() => view && copy('json', JSON.stringify(view.line, null, 2))}>
        <Copy /> {copied === 'json' ? 'Copied' : 'Copy call as JSON'}
      </Button>
      <Button type="button" variant="ghost" size="sm" disabled={!view} onClick={() => view && copy('meta', metaText(view.line))}>
        <Copy /> {copied === 'meta' ? 'Copied' : 'Copy metadata'}
      </Button>
    </div>
  )
}

/**
 * The full trail of one llm_call (DECISIONS-14 §2): what the model got, the tools offered, and its answer. God view only:
 * the requests hold secret-channel text and restricted news. Esc or Close shuts it. The drawer is not modal (FE-realbackend
 * finding 4): the page behind it stays readable and in use, and a click outside does not close it, so the reader can open
 * the trail of another call from the Log while it is open. `seqs` are the calls of the run in order: the arrow keys and the
 * Previous and Next buttons walk them, and a call can be copied as JSON.
 */
export function TrailDrawer({ runId, seq, seqs = [], onSelect, onClose }: { runId: string; seq: number | null; seqs?: number[]; onSelect?: (seq: number) => void; onClose: () => void }) {
  const q = useCallTrail(runId, seq)
  const keys = (e: KeyboardEvent) => {
    const t = e.target as HTMLElement
    if (!onSelect || seq === null || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable) return
    const at = seqs.indexOf(seq)
    const to = e.key === 'ArrowLeft' ? seqs[at - 1] : e.key === 'ArrowRight' ? seqs[at + 1] : undefined
    if (at !== -1 && to !== undefined) {
      e.preventDefault()
      onSelect(to)
    }
  }
  return (
    <Dialog modal={false} open={seq !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent onKeyDown={keys} onInteractOutside={(e) => e.preventDefault()} className="top-0 right-0 left-auto h-dvh max-w-[min(40rem,100%)] translate-x-0 translate-y-0 content-start overflow-y-auto rounded-none sm:max-w-[min(40rem,100%)]">
        <DialogTitle>Trail of call {seq}</DialogTitle>
        <DialogDescription className="text-xs">The request, the tools offered and the answer, as stored. The trail shows the condition: do not put it in a blinded set.</DialogDescription>
        {seq !== null && onSelect && <Toolbar view={q.data} seq={seq} seqs={seqs} onSelect={onSelect} />}
        {seq !== null && <Body q={q} />}
      </DialogContent>
    </Dialog>
  )
}
