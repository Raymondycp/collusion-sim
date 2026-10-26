import { useCallback, useSyncExternalStore } from 'react'

// "Hide" is a view filter for this viewer only (DECISIONS-5 §3). The ledger never changes.
const KEY = 'runs.hidden.v1'
const listeners = new Set<() => void>()
let cache: ReadonlySet<string> | null = null

function read(): ReadonlySet<string> {
  if (cache) return cache
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) ?? '[]') as unknown
    cache = new Set(Array.isArray(raw) ? raw.filter((x): x is string => typeof x === 'string') : [])
  } catch {
    cache = new Set()
  }
  return cache
}

function write(next: ReadonlySet<string>) {
  cache = next
  try {
    localStorage.setItem(KEY, JSON.stringify([...next]))
  } catch {
    /* storage blocked: the choice lasts for this page only */
  }
  for (const fn of listeners) fn()
}

const subscribe = (fn: () => void) => {
  listeners.add(fn)
  return () => void listeners.delete(fn)
}

export function useHiddenRuns() {
  const hidden = useSyncExternalStore(subscribe, read)
  const toggle = useCallback((id: string) => {
    const next = new Set(read())
    if (!next.delete(id)) next.add(id)
    write(next)
  }, [])
  return { hidden, toggle }
}
