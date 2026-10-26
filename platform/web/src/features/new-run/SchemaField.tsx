import { enumLabel } from '@/lib/enumLabels'
import { createContext, useContext, useState, type ReactNode, type SelectHTMLAttributes, type TextareaHTMLAttributes } from 'react'
import { Lock } from 'lucide-react'
import { Input } from '@/components/ui/input'
import { capsText } from '@/components/budget'
import { cn } from '@/lib/utils'
import { defaultFor, flatten, kindOf, pathKey, unwrapNull, type JSONSchema, type Path } from '@/lib/jsonSchema'

export type FormCtx = {
  root: JSONSchema
  research: boolean
  errors: Record<string, string>
  set: (path: Path, value: unknown) => void
  /** In a research run, the value that the condition sets for a locked path. */
  lockedValue: (path: Path) => unknown
}

export const Form = createContext<FormCtx | null>(null)
export function useForm() {
  const c = useContext(Form)
  if (!c) throw new Error('useForm outside <Form>')
  return c
}

export const fieldId = (path: Path) => `f-${pathKey(path)}`

/**
 * The null item of a nullable enum. "None" misled: the memory summary has a value `none`, and a null field never means
 * "nothing" (null transparency, call mode, observation mode and memory summary all take a default, FE-realbackend finding 3).
 */
/** The per-run budget needs one of these caps before the server estimates or starts a run (UX review 2, item 5). */
export const NO_CAP_TEXT = 'No per-run cap set. Add Max calls, Max tokens, or Max USD before estimating.'
/** The server's validation text for an empty budget. */
export const NO_CAP_RE = /set at least one of max_usd/i
export const hasCap = (b: unknown) => ['max_usd', 'max_calls', 'max_tokens'].some((k) => (b as Record<string, unknown> | null | undefined)?.[k] != null)

export const NULL_LABEL = 'Default (not set)'

/** True when a server error points at this path or below it, so a folded section opens to show it. */
export function useHasErrorUnder(path: Path) {
  const k = pathKey(path)
  return Object.keys(useForm().errors).some((e) => e === k || e.startsWith(`${k}.`))
}

export const humanize = (name: string) => {
  const s = name.replace(/_/g, ' ')
  return s.charAt(0).toUpperCase() + s.slice(1)
}

const CONTROL = 'w-full rounded-md border border-input bg-transparent px-2.5 py-1.5 text-sm shadow-xs outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:opacity-60 dark:bg-input/30'

export function NativeSelect({ className, ...p }: SelectHTMLAttributes<HTMLSelectElement>) {
  return <select className={cn(CONTROL, 'h-9', className)} {...p} />
}

export function TextArea({ className, ...p }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className={cn(CONTROL, 'min-h-20 font-mono text-xs', className)} {...p} />
}

export function FieldError({ path }: { path: Path }) {
  const msg = useForm().errors[pathKey(path)]
  return msg ? (
    <p role="alert" className="text-xs text-red-600 dark:text-red-400">
      {msg}
    </p>
  ) : null
}

export function FieldShell({ path, label, help, children, inline = true }: { path: Path; label: ReactNode; help?: ReactNode; children: ReactNode; inline?: boolean }) {
  /** Default: label and control on one row. Pass inline={false} for textarea / JSON. */
  return (
    <div className={cn(inline ? 'grid grid-cols-[6.5rem_minmax(0,1fr)] items-center gap-x-2 gap-y-0.5' : 'space-y-1')}>
      <label htmlFor={fieldId(path)} className={cn('text-xs font-medium text-muted-foreground', inline && 'leading-none')}>
        {label}
      </label>
      <div className={cn('min-w-0', !inline && 'w-full')}>{children}</div>
      {help ? <p className={cn('text-xs text-muted-foreground', inline && 'col-start-2')}>{help}</p> : null}
      <div className={cn(inline && 'col-start-2')}>
        <FieldError path={path} />
      </div>
    </div>
  )
}

const show = (v: unknown) => (v === null || v === undefined ? 'none' : typeof v === 'string' ? v : JSON.stringify(v))

export function LockBadge() {
  return (
    <span className="inline-flex items-center gap-1 rounded border px-1 text-[10px] font-normal text-muted-foreground" title="A research run takes this value from its condition.">
      <Lock className="size-2.5" aria-hidden /> set by condition
    </span>
  )
}

/** A value that the condition sets. Shown, never editable. */
export function LockedValue({ label, value }: { label: ReactNode; value: unknown }) {
  const rows = value !== null && typeof value === 'object' ? Object.entries(flatten(value)) : null
  if (!rows) {
    return (
      <div className="grid grid-cols-[6.5rem_minmax(0,1fr)] items-center gap-x-2">
        <div className="flex items-center gap-1 text-xs font-medium text-muted-foreground">
          {label}
          <LockBadge />
        </div>
        <p className="min-w-0 rounded-md bg-muted/50 px-2 py-1 text-sm whitespace-pre-wrap [overflow-wrap:anywhere]">{show(value)}</p>
      </div>
    )
  }
  return (
    <div className="space-y-1">
      <div className="flex items-center gap-1.5 text-sm font-medium">
        {label}
        <LockBadge />
      </div>
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 rounded-md bg-muted/50 px-2.5 py-1.5 font-mono text-xs">
        {rows.length === 0 && <dd className="col-span-2">none</dd>}
        {rows.map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="text-muted-foreground">{k}</dt>
            <dd className="break-all">{show(v)}</dd>
          </div>
        ))}
      </dl>
    </div>
  )
}

function numberHint(s: JSONSchema, v: unknown): string | null {
  if (typeof v !== 'number') return null
  if (s.type === 'integer' && !Number.isInteger(v)) return 'Enter a whole number.'
  if (s.minimum !== undefined && v < s.minimum) return `The minimum is ${s.minimum}.`
  if (s.exclusiveMinimum !== undefined && v <= s.exclusiveMinimum) return `Enter more than ${s.exclusiveMinimum}.`
  if (s.maximum !== undefined && v > s.maximum) return `The maximum is ${s.maximum}.`
  return null
}

function JsonField({ path, label, value }: { path: Path; label: ReactNode; value: unknown }) {
  const { set } = useForm()
  const [text, setText] = useState(() => JSON.stringify(value ?? null, null, 2))
  const [bad, setBad] = useState(false)
  return (
    <FieldShell path={path} label={label} help="Edit as JSON." inline={false}>
      <TextArea
        id={fieldId(path)}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onBlur={() => {
          try {
            set(path, JSON.parse(text))
            setBad(false)
          } catch {
            setBad(true)
          }
        }}
      />
      {bad && <p className="text-xs text-red-600 dark:text-red-400">This is not valid JSON. The last valid value is kept.</p>}
    </FieldShell>
  )
}

type Props = { path: Path; name: string; schema: JSONSchema; value: unknown; required?: boolean; locked?: boolean; hide?: string[] }

/** One field, built from its JSON Schema. Objects recurse; unknown shapes get a JSON editor. */
export function SchemaField({ path, name, schema, value, required, locked, hide = [] }: Props) {
  const ctx = useForm()
  const { schema: s, nullable } = unwrapNull(ctx.root, schema)
  const label = s.title ?? humanize(name)
  const id = fieldId(path)

  if (locked || (ctx.research && s['x-research-locked'])) return <LockedValue label={label} value={ctx.lockedValue(path) ?? value} />

  const kind = kindOf(s)
  if (kind === 'object') {
    // An optional object that may not be null (memory) shows its fields with their defaults.
    const on = nullable ? value !== null && value !== undefined : true
    return (
      <fieldset className="space-y-3 rounded-lg border p-3">
        <legend className="px-1 text-sm font-medium">{label}</legend>
        {path.join('.') === 'budget' ? (
          // One line for the cap state (UX review 3, item 5): the schema text said "At least one is set" beside the no-cap note.
          <p role="note" className={hasCap(value) ? 'text-xs text-muted-foreground' : 'rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-xs'}>
            {hasCap(value) ? `Per-run caps set: ${capsText(value as Parameters<typeof capsText>[0])}. A call with no price is not seen by Max USD; Max calls and Max tokens still see it.` : NO_CAP_TEXT}
          </p>
        ) : (
          s.description && <p className="text-xs text-muted-foreground">{s.description}</p>
        )}
        {nullable && (
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" className="size-4" checked={on} onChange={(e) => ctx.set(path, e.target.checked ? defaultFor(ctx.root, { ...s, default: undefined }) : null)} />
            Set {label.toLowerCase()}
          </label>
        )}
        {on &&
          Object.entries(s.properties ?? {})
            .filter(([k]) => !hide.includes(k))
            .map(([k, p]) => (
              <SchemaField key={k} path={[...path, k]} name={k} schema={p} value={(value as Record<string, unknown> | undefined)?.[k]} required={(s.required ?? []).includes(k)} />
            ))}
        <FieldError path={path} />
      </fieldset>
    )
  }

  if (kind === 'enum') {
    const opts = s.enum!
    const idx = opts.findIndex((o) => o === value)
    return (
      <FieldShell path={path} label={label} help={s.description}>
        <NativeSelect id={id} value={idx === -1 ? '' : String(idx)} onChange={(e) => ctx.set(path, e.target.value === '' ? (nullable ? null : undefined) : opts[Number(e.target.value)])}>
          {(nullable || idx === -1) && <option value="">{nullable ? NULL_LABEL : 'Select…'}</option>}
          {opts.map((o, i) => (
            <option key={i} value={i} title={String(o)} data-raw={String(o)}>
              {enumLabel(path.join('.'), o)}
            </option>
          ))}
        </NativeSelect>
      </FieldShell>
    )
  }

  if (kind === 'boolean') {
    return (
      <div className="grid grid-cols-[6.5rem_minmax(0,1fr)] items-center gap-x-2 gap-y-0.5">
        <span className="text-xs font-medium leading-none text-muted-foreground">{label}</span>
        <label className="flex min-w-0 items-center gap-2 text-sm">
          <input id={id} type="checkbox" className="size-4" checked={Boolean(value ?? s.default)} onChange={(e) => ctx.set(path, e.target.checked)} />
          <span className="sr-only">{label}</span>
        </label>
        {s.description && <p className="col-start-2 text-xs text-muted-foreground">{s.description}</p>}
        <div className="col-start-2">
          <FieldError path={path} />
        </div>
      </div>
    )
  }

  if (kind === 'number' || kind === 'integer') {
    const hint = numberHint(s, value)
    return (
      <FieldShell path={path} label={label} help={s.description}>
        <Input
          id={id}
          type="number"
          step={kind === 'integer' ? 1 : 'any'}
          min={s.minimum}
          max={s.maximum}
          placeholder={s.default === null ? 'Not set' : s.default !== undefined ? String(s.default) : undefined}
          value={typeof value === 'number' ? value : ''}
          aria-invalid={hint ? true : undefined}
          onChange={(e) => ctx.set(path, e.target.value === '' ? (nullable ? null : required ? s.default : undefined) : Number(e.target.value))}
        />
        {hint && <p className="text-xs text-red-600 dark:text-red-400">{hint}</p>}
      </FieldShell>
    )
  }

  if (kind === 'string') {
    const long = /prompt|notes|instruction/.test(name) || (s.maxLength ?? 0) > 300
    const common = {
      id,
      value: typeof value === 'string' ? value : '',
      maxLength: s.maxLength,
      onChange: (e: { target: { value: string } }) => ctx.set(path, e.target.value === '' ? (nullable ? null : undefined) : e.target.value),
    }
    return (
      <FieldShell path={path} label={label} help={s.description} inline={!long}>
        {long ? <TextArea {...common} className="font-sans text-sm" /> : <Input {...common} />}
      </FieldShell>
    )
  }

  if (kind === 'array') {
    const item = s.items ? unwrapNull(ctx.root, s.items).schema : {}
    const ik = kindOf(item)
    if (ik === 'string' || ik === 'integer' || ik === 'number') {
      const list = Array.isArray(value) ? value : []
      return <ListField path={path} label={label} help={s.description} list={list} numeric={ik !== 'string'} />
    }
  }

  return <JsonField path={path} label={label} value={value} />
}

/** A list of strings or numbers, typed as one comma-separated line. */
function ListField({ path, label, help, list, numeric }: { path: Path; label: ReactNode; help?: string; list: unknown[]; numeric: boolean }) {
  const { set } = useForm()
  const [text, setText] = useState(list.join(', '))
  return (
    <FieldShell path={path} label={label} help={help ?? 'Separate items with commas.'}>
      <Input
        id={fieldId(path)}
        value={text}
        onChange={(e) => {
          setText(e.target.value)
          const parts = e.target.value.split(',').map((x) => x.trim()).filter(Boolean)
          set(path, numeric ? parts.map(Number).filter((n) => Number.isFinite(n)) : parts)
        }}
      />
    </FieldShell>
  )
}

/** A scenario hash with a one-click copy, so it can go into a note or a bug report (UX review item 7). */
export function CopyHash({ hash }: { hash: string }) {
  const [done, setDone] = useState(false)
  return (
    <span className="inline-flex items-center gap-1.5">
      Hash <code className="font-mono">{hash}</code>
      <button
        type="button"
        className="rounded border px-1 text-[10px] hover:bg-muted"
        onClick={() => {
          void navigator.clipboard?.writeText(hash).then(() => setDone(true), () => undefined)
        }}
      >
        {done ? 'Copied' : 'Copy'}
      </button>
    </span>
  )
}
