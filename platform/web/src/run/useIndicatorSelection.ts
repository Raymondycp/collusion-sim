import { useCallback, useSyncExternalStore } from 'react'
import type { IndicatorSelection } from '@/api/indicators'

// A viewer preference (DECISIONS-3 §5), not research data. Kept in localStorage only.
const KEY = 'indicators.v1'
const listeners = new Set<() => void>()
let cache: IndicatorSelection[] | null = null

function read(): IndicatorSelection[] {
  if (cache) return cache
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) ?? '[]') as unknown
    cache = Array.isArray(raw) ? raw.filter((x): x is IndicatorSelection => typeof x?.id === 'string' && typeof x?.params === 'object') : []
  } catch {
    cache = []
  }
  return cache
}

function write(next: IndicatorSelection[]) {
  cache = next
  try {
    localStorage.setItem(KEY, JSON.stringify(next))
  } catch {
    /* storage blocked: the choice lasts for this page only */
  }
  for (const fn of listeners) fn()
}

const subscribe = (fn: () => void) => {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

export function useIndicatorSelection() {
  const selection = useSyncExternalStore(subscribe, read)
  const upsert = useCallback((s: IndicatorSelection) => {
    const cur = read()
    write(cur.some((x) => x.id === s.id) ? cur.map((x) => (x.id === s.id ? s : x)) : [...cur, s])
  }, [])
  const remove = useCallback((id: string) => write(read().filter((x) => x.id !== id)), [])
  return { selection, upsert, remove }
}
