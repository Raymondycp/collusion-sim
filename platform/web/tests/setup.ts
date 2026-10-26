import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, vi } from 'vitest'
import { configure } from '@testing-library/react'

// isolate: false keeps one jsdom per worker (DECISIONS-13 §4.2). Each test file still gets fresh app modules,
// so mock run lists, id counters, live-run clocks and each file's vi.mock start clean.
vi.resetModules()

// findBy and waitFor wait 1 s by default. On a busy machine a page can need more, and the same test then fails at random
// (seeds 1 and 2 on 22 Oct: a different test each run). A longer wait slows only a test that fails.
configure({ asyncUtilTimeout: 5_000 })

// The jsdom of a worker is shared by all its test files, so no test may leave a spy, a fake clock, a stub or a stored value behind.
// jsdom has no layout observers. Radix and the chart panels need one, so each test gets a fresh stub.
class Observer {
  observe() {}
  unobserve() {}
  disconnect() {}
}
beforeEach(() => {
  vi.stubGlobal('ResizeObserver', Observer)
  vi.stubGlobal('IntersectionObserver', Observer)
})
afterEach(() => {
  vi.restoreAllMocks()
  vi.useRealTimers()
  vi.unstubAllGlobals()
  localStorage.clear()
  sessionStorage.clear()
})
