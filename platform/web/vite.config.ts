import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { msw } from 'msw/vite'
import path from 'node:path'
import { siteGate } from './site-gate'
import { snapshotData } from './snapshot-data'

const backend = process.env.SIM_BACKEND ?? 'http://127.0.0.1:8000'
// Tests run in a shuffled order. The seed is printed, so an order bug can run again with VITEST_SEED=<seed> (QUEUE rule e).
const seed = Number(process.env.VITEST_SEED) || Math.floor(Math.random() * 1_000_000)
if (process.env.VITEST) console.log(`vitest seed ${seed} (run again: VITEST_SEED=${seed})`)

export default defineConfig(({ mode }) => ({
  // snapshot: a static, read-only copy of recorded API answers (scripts/record-snapshot.mjs); no proxy and no site gate.
  plugins: [
    react(),
    tailwindcss(),
    ...(mode === 'snapshot' ? [snapshotData()] : [siteGate()]),
    ...(mode === 'mock' || mode === 'snapshot' ? [msw({ mode: 'worker-only' })] : []),
  ],
  build: mode === 'snapshot' ? { outDir: 'dist-snapshot' } : undefined,
  resolve: { alias: { '@': path.resolve(import.meta.dirname, 'src') } },
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    allowedHosts: true,
    fs: { allow: ['.', '../docs/sim/api/examples'] },
    proxy: mode === 'mock' || mode === 'snapshot' ? undefined : { '/api': { target: backend, ws: true, changeOrigin: false } },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./tests/setup.ts'],
    include: ['tests/**/*.test.{ts,tsx}'],
    // More workers time out jsdom tests when the machine is busy (CR-35).
    maxWorkers: 2,
    // One jsdom per worker, not per file: less memory and time on a low-memory machine (DECISIONS-13 §4.2).
    isolate: false,
    sequence: { shuffle: true, seed },
    // A busy machine (full suite, 2 workers) can take more than 5 s for one page test; see the wait limit in tests/setup.ts.
    testTimeout: 20_000,
  },
}))
