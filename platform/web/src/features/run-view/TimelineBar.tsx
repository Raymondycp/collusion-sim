import { useEffect, useState } from 'react'
import { ChevronFirst, ChevronLast, Pause, Play, StepBack, StepForward } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Slider } from '@/components/ui/slider'
import type { Marker } from '@/run/fold'

const SPEEDS = [1, 4, 10] as const
const JUMP_KINDS = new Set<Marker['kind']>(['news', 'probe', 'placebo', 'ablation', 'fallback', 'budget', 'fork'])

type Props = {
  minT: number
  maxT: number
  /** null = follow the latest round. */
  t: number | null
  onChange: (t: number | null) => void
  markers: Marker[]
  following: boolean
}

export function TimelineBar({ minT, maxT, t, onChange, markers, following }: Props) {
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeed] = useState<(typeof SPEEDS)[number]>(1)
  const at = t ?? maxT
  // Rounds start at 1, so a maximum of 0 means no round has settled yet.
  const empty = maxT < Math.max(minT, 1)
  // With one round there is nothing to scrub. Radix also divides by max − min, so the range must not be 0.
  const single = maxT <= minT

  useEffect(() => {
    if (!playing) return
    const id = setInterval(() => {
      if (at >= maxT) {
        setPlaying(false)
        return
      }
      onChange(at + 1)
    }, 1000 / speed)
    return () => clearInterval(id)
  }, [playing, speed, at, maxT, onChange])

  const jumps = markers.filter((m) => JUMP_KINDS.has(m.kind))
  const step = (d: number) => {
    setPlaying(false)
    onChange(Math.min(maxT, Math.max(minT, at + d)))
  }

  return (
    <div className="flex flex-col gap-2 rounded-lg border p-2 sm:flex-row sm:items-center" role="group" aria-label="Timeline">
      <div className="flex items-center gap-1">
        <Button variant="ghost" size="icon" aria-label="First round" disabled={empty} onClick={() => (setPlaying(false), onChange(minT))}>
          <ChevronFirst />
        </Button>
        <Button variant="ghost" size="icon" aria-label="Previous round" disabled={empty || at <= minT} onClick={() => step(-1)}>
          <StepBack />
        </Button>
        <Button
          variant="ghost"
          size="icon"
          aria-label={playing ? 'Pause replay' : 'Play replay'}
          disabled={empty}
          onClick={() => {
            if (!playing && at >= maxT) onChange(minT)
            setPlaying(!playing)
          }}
        >
          {playing ? <Pause /> : <Play />}
        </Button>
        <Button variant="ghost" size="icon" aria-label="Next round" disabled={empty || at >= maxT} onClick={() => step(1)}>
          <StepForward />
        </Button>
        <Button
          variant={following ? 'secondary' : 'ghost'}
          size="icon"
          aria-label="Follow the latest round"
          aria-pressed={following}
          disabled={empty}
          onClick={() => (setPlaying(false), onChange(null))}
        >
          <ChevronLast />
        </Button>
      </div>

      <div className="flex min-w-0 flex-1 items-center gap-3 px-1">
        {/* While playing, the counter changes every tick; announcing each one floods a screen reader. */}
        <span className="w-16 shrink-0 text-sm tabular-nums" aria-live={playing ? 'off' : 'polite'}>
          R{empty ? '–' : at}
          <span className="text-muted-foreground">/{empty ? '–' : maxT}</span>
        </span>
        <Slider
          thumbProps={{ 'aria-label': 'Round', 'aria-valuetext': empty ? 'No rounds yet' : `Round ${at} of ${maxT}` }}
          min={minT}
          max={single ? minT + 1 : maxT}
          step={1}
          value={[at]}
          disabled={empty || single}
          onValueChange={([v]) => (setPlaying(false), onChange(v === maxT && following ? null : v!))}
        />
      </div>

      <div className="flex items-center gap-2">
        <Select value={String(speed)} onValueChange={(v) => setSpeed(Number(v) as (typeof SPEEDS)[number])}>
          <SelectTrigger className="w-20" aria-label="Replay speed">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {SPEEDS.map((s) => (
              <SelectItem key={s} value={String(s)}>
                ×{s}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select value="" onValueChange={(v) => (setPlaying(false), onChange(Number(v)))} disabled={jumps.length === 0}>
          <SelectTrigger className="w-full sm:w-44" aria-label="Jump to an event">
            <SelectValue placeholder="Jump to event" />
          </SelectTrigger>
          <SelectContent>
            {jumps.map((m) => (
              <SelectItem key={m.seq} value={String(m.t)}>
                R{m.t} · {m.kind}
                {m.agentId ? ` ${m.agentId}` : ''}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
    </div>
  )
}
