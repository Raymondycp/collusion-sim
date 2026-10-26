import { useCallback, useMemo } from 'react'
import { useSearchParams } from 'react-router'

/** The replay cursor and the branch overlays live in the URL, so a link opens the same view. */
export function useViewParams() {
  const [params, setParams] = useSearchParams()
  // A round is a whole number. Anything else in the URL (?t=abc, ?t=-1) means "latest", not NaN.
  const rawT = params.get('t')
  const t = rawT !== null && /^\d+$/.test(rawT) ? Number(rawT) : null
  const overlay = params.get('overlay')
  const overlayIds = useMemo(() => (overlay ? overlay.split(',').filter(Boolean) : []), [overlay])
  const setT = useCallback(
    (v: number | null) =>
      setParams(
        (p) => {
          if (v === null) p.delete('t')
          else p.set('t', String(v))
          return p
        },
        { replace: true },
      ),
    [setParams],
  )
  const toggleOverlay = useCallback(
    (id: string) =>
      setParams((p) => {
        const cur = (p.get('overlay') ?? '').split(',').filter(Boolean)
        const next = cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]
        if (next.length) p.set('overlay', next.join(','))
        else p.delete('overlay')
        return p
      }),
    [setParams],
  )
  return { t, setT, overlayIds, toggleOverlay }
}
