import { useEffect, useSyncExternalStore } from 'react'

export type Theme = 'light' | 'dark'
const KEY = 'theme'

function readStored(): Theme | null {
  try {
    const v = localStorage.getItem(KEY)
    return v === 'light' || v === 'dark' ? v : null
  } catch {
    return null
  }
}

const media = () => matchMedia('(prefers-color-scheme: dark)')
const current = (): Theme => (document.documentElement.classList.contains('dark') ? 'dark' : 'light')

function subscribe(fn: () => void) {
  const obs = new MutationObserver(fn)
  obs.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })
  return () => obs.disconnect()
}

/** The active theme. Components that draw on canvas use it to re-read the colour tokens. */
export const useTheme = () => useSyncExternalStore(subscribe, current)

export function setTheme(t: Theme) {
  document.documentElement.classList.toggle('dark', t === 'dark')
  try {
    localStorage.setItem(KEY, t)
  } catch {
    /* private mode: the choice lasts for this page only */
  }
}

/** Follows the OS theme until the viewer picks one. */
export function useFollowSystemTheme() {
  useEffect(() => {
    const m = media()
    const on = () => {
      if (!readStored()) document.documentElement.classList.toggle('dark', m.matches)
    }
    m.addEventListener('change', on)
    return () => m.removeEventListener('change', on)
  }, [])
}

export function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}
