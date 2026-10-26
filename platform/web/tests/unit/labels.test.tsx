import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { CopyHash } from '@/features/new-run/SchemaField'
import type { RunDetail, RunSummary } from '@/api/types'
import { branchLabel, conditionText } from '@/features/workbench/labels'
import { SettingSummary } from '@/features/workbench/Panels'
import { readCallsText } from '@/features/conditions/limits'
import { foldEvents } from '@/run/fold'

afterEach(cleanup)

const branch = (type: string) => ({ parent_run_id: 'run_P', fork_t: 10, intervention: { type } }) as unknown as RunSummary['branch']

describe('condition of a run (A7 finding 11)', () => {
  it('an ablation child shows its parent condition and "ablated"; other runs show their own', () => {
    expect(conditionText({ condition_id: null, branch: branch('ablation') }, { condition_id: 'C1' })).toBe('C1 ablated')
    expect(conditionText({ condition_id: 'C1', branch: branch('probe') }, { condition_id: 'C3' })).toBe('C1')
    expect(conditionText({ condition_id: null, branch: branch('ablation') }, undefined)).toBeNull()
    expect(conditionText({ condition_id: null, branch: branch('probe') }, { condition_id: 'C1' })).toBeNull()
    expect(conditionText({ condition_id: null, branch: null }, undefined)).toBeNull()
  })

  it('the Setting line says the condition, else the run kind', () => {
    const run = { kind: 'research', seed: 1, split: 'pilot', max_rounds: 24, scenario_id: 's', config: {} } as unknown as RunDetail
    const { rerender } = render(<SettingSummary run={run} condition="C1 ablated" state={foldEvents([])} />)
    expect(screen.getByLabelText('Setting')).toHaveTextContent('condition C1 ablated')
    rerender(<SettingSummary run={run} condition={null} state={foldEvents([])} />)
    expect(screen.getByLabelText('Setting')).toHaveTextContent(/Setting · research · seed 1/)
  })
})

describe('the Setting chips and the copyable hash (UX review item 7)', () => {
  const run = { kind: 'research', seed: 1, split: 'dev', max_rounds: 24, scenario_id: 's', config: { limits: { max_calls: 50 } } } as unknown as RunDetail

  it('each chip has a reason in its tooltip, and the limits chip lists the caps', () => {
    render(<SettingSummary run={run} condition="C1" state={foldEvents([])} />)
    const chips = [...screen.getByLabelText('Setting').querySelectorAll('span[title]')]
    expect(chips.map((c) => c.textContent)).toEqual(['condition C1', 'seed 1', 'split dev', 'rounds 0 of 24', 'scenario s', 'limits max_calls 50'])
    for (const c of chips) expect(c.getAttribute('title')).toMatch(/[a-z]{4}/)
    expect(chips.find((c) => c.textContent === 'split dev')!.getAttribute('title')).toMatch(/test seeds run once/)
  })

  it('a run with no limits has no limits chip', () => {
    render(<SettingSummary run={{ ...run, config: {} } as unknown as RunDetail} condition="C1" state={foldEvents([])} />)
    expect(screen.getByLabelText('Setting')).not.toHaveTextContent('limits')
  })

  it('Copy writes the hash to the clipboard and then says Copied; a refusing clipboard leaves it at Copy', async () => {
    const writeText = vi.fn(() => Promise.resolve())
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    const { unmount } = render(<CopyHash hash="sha256:abc" />)
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    expect(writeText).toHaveBeenCalledWith('sha256:abc')
    expect(await screen.findByRole('button', { name: 'Copied' })).toBeInTheDocument()
    unmount()
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText: vi.fn(() => Promise.reject(new Error('no'))) } })
    render(<CopyHash hash="sha256:abc" />)
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: 'Copied' })).toBeNull()
    vi.unstubAllGlobals()
  })
})

describe('planned child labels (A7 finding 7)', () => {
  const plan = { probe: { at: 20, rounds: 5, target: 'B' }, placebo: { at: 20, rounds: 5, n: 3 }, ablation: { at: 10, mode: 'shuffle' as const } }

  it('name the fork round and the change, from the plan, not the plan index', () => {
    expect(branchLabel({ kind: 'probe', index: 0 }, plan)).toBe('probe B at R20')
    expect(branchLabel({ kind: 'placebo', index: 10 }, plan)).toBe('placebo 1 of 3 at R20')
    expect(branchLabel({ kind: 'placebo', index: 12 }, plan)).toBe('placebo 3 of 3 at R20')
    expect(branchLabel({ kind: 'ablation', index: 20 }, plan)).toBe('ablation (shuffle) from R10')
    expect(branchLabel({ kind: 'ablation', index: 21 }, plan)).toBe('ablation (shuffle) 2 from R10')
  })

  it('a null ablation (the condition has no private channel) names no mode (CR-63 3)', () => {
    expect(branchLabel({ kind: 'ablation', index: 20 }, plan, { privateChannel: false })).toBe('ablation from R10')
    expect(branchLabel({ kind: 'probe', index: 0 }, plan, { privateChannel: false })).toBe('probe B at R20')
  })

  it('leave out what the plan does not give', () => {
    expect(branchLabel({ kind: 'probe', index: 0 }, undefined)).toBe('probe')
    expect(branchLabel({ kind: 'placebo', index: 11 }, {})).toBe('placebo 2')
    expect(branchLabel({ kind: 'ablation', index: 20 }, {})).toBe('ablation')
  })
})

describe('read calls per round (A7 finding 8)', () => {
  it('shows the smaller of the condition limit and the tool grant, and both when they differ', () => {
    expect(readCallsText({ max_queries_per_round: 0 }, { grants: [], max_queries_per_round: 3 })).toBe('No read calls (limit 0)')
    expect(readCallsText({ max_queries_per_round: 3 }, { grants: [], max_queries_per_round: 0 })).toBe('No read calls (tool grant 0)')
    expect(readCallsText({ max_queries_per_round: 1 }, { grants: [] })).toBe('Read calls per round: 1 (limit 1, tool grant 3)')
    expect(readCallsText({}, { grants: [], max_queries_per_round: 2 })).toBe('Read calls per round: 2 (limit 3, tool grant 2)')
    expect(readCallsText({ max_queries_per_round: 3 }, { grants: [] })).toBe('Read calls per round: 3')
  })
})
