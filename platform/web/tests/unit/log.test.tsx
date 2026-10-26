import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { makeEpisode } from '@/mocks/episode'
import { foldEvents } from '@/run/fold'
import { EventLog } from '@/features/workbench/Panels'

afterEach(cleanup)

const run = (conditionId: string) => foldEvents(makeEpisode({ runId: 'run_LOG', kind: 'research', seed: 101, rounds: 12, start: '2026-10-23T09:00:00Z', conditionId, branch: null }))
const c1 = run('C1')
const silent = run('C3-silent')

const view = (state = c1, cursorT = 6, onTrail?: (seq: number) => void) => render(<EventLog state={state} cursorT={cursorT} onPickT={vi.fn()} publicView={false} onTrail={onTrail} />)
const log = () => screen.getByRole('list', { name: 'Event log' })
const rows = () => within(log()).getAllByRole('listitem').filter((li) => !li.querySelector('[aria-expanded]') && li.querySelector('button'))
const chip = (name: string | RegExp) => screen.getByRole('button', { name, pressed: undefined })

describe('event log: rounds, folding and filters (UX review item 4)', () => {
  it('has a header for each round, newest first, and marks the round at the scrubber', () => {
    view(c1, 6)
    const items = within(log()).getAllByRole('listitem').filter((li) => li.querySelector('[aria-expanded]'))
    expect(items.map((li) => li.textContent?.match(/R(\d+)/)?.[1])).toEqual(['12', '11', '10', '9', '8', '7', '6', '5', '4', '3', '2', '1', '0'].filter((r) => items.some((li) => li.textContent?.includes(`R${r} `))))
    const current = items.filter((li) => li.getAttribute('aria-current') === 'true')
    expect(current).toHaveLength(1)
    expect(current[0]).toHaveTextContent(/^▾R6 · \d+ events · at the scrubber$/)
  })

  it('a header folds and opens its rows', () => {
    view(c1, 6)
    const header = within(log()).getAllByRole('button', { name: /^R12 · / })[0]!
    const before = rows().length
    fireEvent.click(header)
    expect(within(log()).getByRole('button', { name: /^R12 · / })).toHaveAttribute('aria-expanded', 'false')
    expect(rows().length).toBeLessThan(before)
    fireEvent.click(within(log()).getByRole('button', { name: /^R12 · / }))
    expect(rows()).toHaveLength(before)
  })

  it('"Fold other rounds" leaves only the round at the scrubber open, and "Open all rounds" gives the rest back', () => {
    view(c1, 6)
    const all = rows().length
    fireEvent.click(screen.getByRole('button', { name: 'Fold other rounds' }))
    const open = within(log()).getAllByRole('button').filter((b) => b.getAttribute('aria-expanded') === 'true')
    expect(open).toHaveLength(1)
    expect(open[0]).toHaveTextContent(/R6 · /)
    expect(rows().every((li) => li.textContent?.includes('R6'))).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Open all rounds' }))
    expect(rows()).toHaveLength(all)
  })

  it('"Model calls only" keeps the model calls, and the count says so', () => {
    view(c1, 6)
    const all = rows().length
    fireEvent.click(chip('Model calls only'))
    expect(rows().length).toBeLessThan(all)
    expect(rows().every((li) => /model call/.test(li.textContent ?? ''))).toBe(true)
    expect(screen.getByText(new RegExp(`^${rows().length} of \\d+ events\\.$`))).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Clear filters' }))
    expect(rows()).toHaveLength(all)
  })

  it('a seller chip keeps that seller, and the Talk and Pricing chips keep one kind of call', () => {
    view(c1, 6)
    fireEvent.click(chip('Seller A'))
    expect(rows().length).toBeGreaterThan(0)
    expect(rows().every((li) => /^R\d+A |A posted|A set|A held|A model call|A fell back/.test(li.textContent ?? ''))).toBe(true)
    fireEvent.click(chip('Seller A'))
    fireEvent.click(chip('Talk'))
    expect(rows().every((li) => /model call \(talk\)/.test(li.textContent ?? ''))).toBe(true)
    fireEvent.click(chip('Pricing'))
    expect(rows().every((li) => /model call \(act\)/.test(li.textContent ?? ''))).toBe(true)
    fireEvent.click(chip('Pricing'))
    expect(rows().length).toBeGreaterThan(0)
  })

  it('a filter with no match says so, and each chip says whether it is on', () => {
    view(silent, 6)
    fireEvent.click(chip('Seller B'))
    fireEvent.click(chip('Talk'))
    expect(screen.getByText('No event matches these filters.')).toBeInTheDocument()
    expect(rows()).toHaveLength(0)
    expect(screen.getByRole('button', { name: 'Seller B' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: 'Talk' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: 'Pricing' })).toHaveAttribute('aria-pressed', 'false')
  })

  it('the Trail buttons are in a fixed column, and a click on a row still moves the scrubber', () => {
    const onPickT = vi.fn()
    const onTrail = vi.fn()
    render(<EventLog state={c1} cursorT={6} onPickT={onPickT} publicView={false} onTrail={onTrail} />)
    const call = rows().find((li) => li.querySelector('[aria-label^="Trail of call"]'))!
    expect(call.querySelector('[class*="4.5rem"]')).toContainElement(call.querySelector('[aria-label^="Trail of call"]') as HTMLElement)
    fireEvent.click(call.querySelector('[aria-label^="Trail of call"]')!)
    expect(onTrail).toHaveBeenCalledTimes(1)
    fireEvent.click(within(call).getAllByRole('button')[0]!)
    expect(onPickT).toHaveBeenCalledWith(expect.any(Number))
  })

  it('an event of an earlier round that comes late joins the header of its round (CR-92 5)', () => {
    const ev = c1.events
    const early = ev.find((e) => e.t === 2 && 'agent_id' in e)!
    const moved = { ...c1, events: [...ev.filter((e) => e !== early), early] }
    const err = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    view(moved, 6)
    expect(within(log()).getAllByRole('button', { name: /^R2 · / })).toHaveLength(1)
    expect(err.mock.calls.filter((c) => String(c[0]).includes('same key'))).toHaveLength(0)
    err.mockRestore()
  })

  it('the public log offers no model-call filters (CR-92 6)', () => {
    render(<EventLog state={c1} cursorT={6} onPickT={vi.fn()} publicView />)
    expect(screen.queryByRole('button', { name: 'Model calls only' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Talk' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Pricing' })).toBeNull()
  })
})
