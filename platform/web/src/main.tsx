import './index.css'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createBrowserRouter, Navigate, RouterProvider, useLocation, useParams } from 'react-router'
import { retryOnce } from '@/api/client'
import { TooltipProvider } from '@/components/ui/tooltip'
import { AppShell } from '@/app/AppShell'
import { RunsPage } from '@/features/runs/RunsPage'
import { WorkbenchLayout } from '@/features/workbench/WorkbenchLayout'
import { AboutPage } from '@/features/about/AboutPage'
import { ProvidersPage } from '@/features/providers/ProvidersPage'
import { NewRunPage } from '@/features/new-run/NewRunPage'
import { ComparePage } from '@/features/compare/ComparePage'
import { ScenariosPage } from '@/features/scenarios/ScenariosPage'
import { ScenarioPage } from '@/features/scenarios/ScenarioPage'
import { ConditionPage, ConditionsPage } from '@/features/conditions/ConditionsPage'
import { EvaluatePage } from '@/features/evaluate/EvaluatePage'
import { ExperimentPage, ExperimentsPage } from '@/features/experiments/ExperimentsPage'
import { NewExperimentPage } from '@/features/experiments/NewExperimentPage'
import { ArchitecturePage } from '@/features/docs/ArchitecturePage'
import { DataPage } from '@/features/docs/DataPage'
import { NotFound } from '@/app/NotFound'
import { SettingsLayout } from '@/features/settings/SettingsLayout'
import { DefaultsPage } from '@/features/settings/DefaultsPage'
import { ConditionFilePage, ConditionsSettingsPage } from '@/features/settings/ConditionsSettingsPage'
import { ResearchFilesPage } from '@/features/settings/ResearchFilesPage'
import { FreezePage } from '@/features/settings/FreezePage'

/** The old run page is gone (handoff simplify-cut B5): every `/runs/<id>` link opens the workbench tab, search kept. */
function RunRedirect() {
  const { runId } = useParams()
  const { search } = useLocation()
  return <Navigate to={`/workbench/${encodeURIComponent(runId ?? '')}${search}`} replace />
}

const router = createBrowserRouter([
  {
    element: <AppShell />,
    children: [
      { index: true, element: <Navigate to="/runs" replace /> },
      { path: 'runs', element: <RunsPage /> },
      { path: 'runs/new', element: <NewRunPage /> },
      { path: 'runs/compare', element: <ComparePage /> },
      {
        path: 'workbench',
        children: [
          { index: true, element: <Navigate to="new" replace /> },
          // One layout instance for every workbench path, so a tab switch keeps the setup draft. The layout renders the pages itself.
          { element: <WorkbenchLayout />, children: [{ path: 'new', element: null }, { path: ':runId', element: null }] },
        ],
      },
      { path: 'runs/:runId', element: <RunRedirect /> },
      { path: 'runs/:runId/*', element: <RunRedirect /> },
      { path: 'scenarios', element: <ScenariosPage /> },
      { path: 'scenarios/new', element: <ScenarioPage /> },
      { path: 'scenarios/:scenarioId', element: <ScenarioPage /> },
      { path: 'conditions', element: <ConditionsPage /> },
      { path: 'conditions/:conditionId', element: <ConditionPage /> },
      { path: 'experiments', element: <ExperimentsPage /> },
      { path: 'experiments/new', element: <NewExperimentPage /> },
      { path: 'experiments/:experimentId', element: <ExperimentPage /> },
      { path: 'evaluations', element: <EvaluatePage /> },
      {
        path: 'settings',
        element: <SettingsLayout />,
        children: [
          { index: true, element: <Navigate to="profiles" replace /> },
          { path: 'profiles', element: <ProvidersPage /> },
          { path: 'defaults', element: <DefaultsPage /> },
          { path: 'conditions', element: <ConditionsSettingsPage /> },
          { path: 'conditions/:conditionId', element: <ConditionFilePage /> },
          { path: 'research', element: <ResearchFilesPage /> },
          { path: 'freeze', element: <FreezePage /> },
        ],
      },
      { path: 'providers', element: <Navigate to="/settings/profiles" replace /> },
      { path: 'architecture', element: <ArchitecturePage /> },
      { path: 'data', element: <DataPage /> },
      // Lazy, so Monaco and the markdown renderer stay out of the main bundle.
      { path: 'files', lazy: () => import('@/features/files/FilesPage').then((m) => ({ Component: m.FilesPage })) },
      { path: 'about', element: <AboutPage /> },
      { path: '*', element: <NotFound /> },
    ],
  },
])

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: retryOnce, refetchOnWindowFocus: false } } })

async function start() {
  if (import.meta.env.MODE === 'mock') {
    const { startMocks } = await import('@/mocks/browser')
    await startMocks()
  }
  if (import.meta.env.MODE === 'snapshot') {
    const { startSnapshot } = await import('@/mocks/snapshotBrowser')
    await startSnapshot()
  }
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        <TooltipProvider>
          <RouterProvider router={router} />
        </TooltipProvider>
      </QueryClientProvider>
    </StrictMode>,
  )
}

void start()
