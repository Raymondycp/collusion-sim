import { Code, DataTable, Flow, RepoLink, Section, type FlowStep } from './Flow'

type BoxProps = { x: number; y: number; w: number; h?: number; title: string; sub?: string }

/** One box of the architecture diagram. Colours come from theme classes, so light and dark both work. */
function Box({ x, y, w, h = 44, title, sub }: BoxProps) {
  return (
    <g>
      <rect x={x} y={y} width={w} height={h} rx={6} className="fill-card stroke-border" />
      <text x={x + w / 2} y={y + (sub ? 19 : h / 2 + 4)} textAnchor="middle" className="fill-foreground text-[12px] font-medium">
        {title}
      </text>
      {sub && (
        <text x={x + w / 2} y={y + 34} textAnchor="middle" className="fill-muted-foreground text-[10px]">
          {sub}
        </text>
      )}
    </g>
  )
}

function Arrow({ d }: { d: string }) {
  return <path d={d} fill="none" markerEnd="url(#arrow)" className="stroke-muted-foreground" />
}

function ArchitectureDiagram() {
  return (
    <svg viewBox="0 0 720 320" role="img" aria-label="Architecture diagram" className="w-full min-w-[640px]">
      <defs>
        <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0,0 L10,5 L0,10 z" className="fill-muted-foreground" />
        </marker>
      </defs>
      <Box x={10} y={20} w={150} title="Web UI" sub="web/ (React, Vite)" />
      <Box x={10} y={84} w={150} title="CLI collusion-sim" sub="src/simcli" />
      <Box x={200} y={52} w={150} h={56} title="FastAPI app" sub="src/api (REST /api, WebSocket)" />
      <Box x={390} y={52} w={150} h={56} title="Services" sub="src/services" />
      <Box x={580} y={40} w={130} title="Storage" sub="SQLite, runs/ files" />
      <Box x={580} y={100} w={130} title="Research inputs" sub="conditions/*.yaml" />
      <Box x={200} y={160} w={140} title="Simulation core" sub="src/simcore" />
      <Box x={360} y={160} w={140} title="Features" sub="src/features, indicators" />
      <Box x={520} y={160} w={140} title="Judge" sub="src/judge" />
      <Box x={360} y={250} w={140} title="LLM adapters" sub="src/adapters/llm" />
      <Box x={560} y={250} w={130} title="LLM providers" sub="per provider profile" />
      <Box x={10} y={250} w={150} title=".env (git-ignored)" sub="keys, SIM_ALLOW_PAID" />
      <Arrow d="M160,42 L180,42 L180,70 L200,70" />
      <Arrow d="M160,106 L180,106 L180,90 L200,90" />
      <Arrow d="M350,80 L390,80" />
      <Arrow d="M540,62 L580,62" />
      <Arrow d="M580,122 L560,122 L560,98 L540,98" />
      <Arrow d="M430,108 L430,130 L270,130 L270,160" />
      <Arrow d="M465,108 L465,160" />
      <Arrow d="M500,108 L500,130 L590,130 L590,160" />
      <Arrow d="M270,204 L270,272 L360,272" />
      <Arrow d="M590,204 L590,230 L440,230 L440,250" />
      <Arrow d="M500,272 L560,272" />
      <Arrow d="M160,272 L360,272" />
    </svg>
  )
}

const DEPLOY: FlowStep[] = [
  { title: 'Developer', note: 'push' },
  { title: 'GitHub main', note: 'branch' },
  { title: 'GitHub Actions', note: 'deploy-oracle.yml, ssh-action' },
  { title: 'Oracle server', note: '/workspace/apart-hackathon' },
  { title: 'scripts/oracle-deploy.sh', note: 'git reset, install, restart' },
  { title: 'systemd user services', note: 'apart-api, apart-web, apart-tunnel' },
  { title: 'apart.orionteches.com', note: 'API at /api' },
]

const PIPELINE: FlowStep[] = [
  { title: 'Conditions freeze', note: 'opens dev seeds', gate: true },
  { title: 'Experiment', note: 'conditions x seeds' },
  { title: 'Judge', note: 'LLM reads the transcript' },
  { title: 'Features export', note: 'one row per parent run' },
  { title: 'Fit rule on dev', note: 'scripts/fit_rule.py fit' },
  { title: 'Rule freeze', note: 'opens test seeds', gate: true },
  { title: 'Test once', note: 'each condition and seed once' },
  { title: 'Apply', note: 'scripts/fit_rule.py apply' },
]

const CLI_EXAMPLES = `uv run collusion-sim health
uv run collusion-sim freeze status
uv run collusion-sim experiments estimate --file exp.json      # no LLM call
uv run collusion-sim experiments start --file exp.json --wait  # paid when sellers use an LLM
uv run collusion-sim experiments export EXP_ID -o exp.zip
uv run collusion-sim evaluations start --file eval.json --wait
uv run collusion-sim features export EXP_ID --layer B --format csv -o dev-features.csv
uv run collusion-sim freeze run --stage rule --approved-by NAME
uv run python scripts/fit_rule.py apply test-features.csv`

const API_GROUPS: [string, string, string][] = [
  ['System', 'GET /health', 'Status, API version, paid_calls_enabled, durable_backend'],
  ['Freeze', 'GET /freeze, POST /freeze', 'Freeze status; freeze stage conditions or rule'],
  ['Research files', 'GET /conditions, GET /conditions/{id}, GET /seeds, PUT /research/{name}', 'Condition files, seed map, research file edits'],
  ['Profiles', 'GET/POST /provider-profiles, PATCH/DELETE /provider-profiles/{id}', 'Provider profiles (keys are write-only)'],
  ['Scenarios', 'GET/POST /scenarios, GET/PUT /scenarios/{id}, POST /market/preview', 'Draft, check and freeze a market scenario'],
  ['Runs', 'POST /runs/estimate, POST /runs, GET /runs, GET /runs/{id}', 'Estimate, start (one run per seed), list, read'],
  ['Run control', 'POST /runs/{id}/pause, /resume, /stop, /branches, /clone', 'Control, branch (probe, placebo, ablation), clone'],
  ['Run data', 'GET /runs/{id}/events, /series, /observations, /state, /episode.jsonl, /export, /transcript', 'Events, charts, replay, exports'],
  ['Experiments', 'POST /experiments/estimate, POST /experiments, GET /experiments/{id}, GET .../features', 'Conditions x seeds, feature table'],
  ['Evaluations', 'POST /evaluations/estimate, POST /evaluations, GET /evaluations/{id}, GET /judge/views', 'LLM judge jobs'],
  ['Registries', '/roles, /tools, /policies, /indicators, /demand-models, /presets, /schema/run-config', 'Catalogues and config schema'],
  ['Records', 'POST /runs/archive, /experiments/archive, GET /ledger, POST /imports', 'Archive (nothing is deleted), call ledger, log import'],
]

export function ArchitecturePage() {
  return (
    <div className="mx-auto max-w-4xl space-y-8">
      <section className="space-y-2">
        <h1 className="text-xl font-semibold">System architecture</h1>
        <p className="text-sm text-muted-foreground">
          The platform runs LLM sellers in a repeated pricing market. Each run is a recorded episode. Source: <RepoLink path="README.md">README.md</RepoLink>. Rules:{' '}
          <RepoLink path="docs/sim/SPEC.md">docs/sim/SPEC.md</RepoLink>. API contract: <RepoLink path="docs/sim/api/openapi.yaml">docs/sim/api/openapi.yaml</RepoLink>.
        </p>
      </section>

      <Section title="Components">
        <div className="overflow-x-auto rounded-lg border p-2">
          <ArchitectureDiagram />
        </div>
        <ul className="list-disc space-y-1 pl-5 text-sm">
          <li>The web UI and the CLI are clients of the API. They show the same data.</li>
          <li>
            The research model of each seller is in <Code>conditions/models.yaml</Code>. It names a provider profile, a model and settings.
          </li>
          <li>
            Provider keys come from the process environment or <Code>.env</Code>. No API response shows a key.
          </li>
          <li>
            DBOS (crash recovery) is off by default. <Code>collusion-sim serve --durable</Code> turns it on.
          </li>
        </ul>
      </Section>

      <Section title="Deployment">
        <Flow steps={DEPLOY} label="Deployment flow" />
        <ul className="list-disc space-y-1 pl-5 text-sm">
          <li>
            The workflow <RepoLink path=".github/workflows/deploy-oracle.yml" /> runs on each push to <Code>main</Code>. It ends with a call to <Code>/api/health</Code> on the public URL.
          </li>
          <li>
            <RepoLink path="scripts/oracle-deploy.sh" /> keeps <Code>.env</Code>, <Code>data/</Code>, <Code>logs/</Code> and <Code>cache/</Code> on the server.
          </li>
          <li>
            The site is{' '}
            <a className="underline underline-offset-4" href="https://apart.orionteches.com" target="_blank" rel="noreferrer noopener">
              https://apart.orionteches.com
            </a>
            . The API is at <Code>/api</Code>.
          </li>
        </ul>
      </Section>

      <Section title="Experiment pipeline">
        <Flow steps={PIPELINE} label="Experiment pipeline" />
        <p className="text-xs text-muted-foreground">A dashed box is a freeze gate.</p>
        <ul className="list-disc space-y-1 pl-5 text-sm">
          <li>Each parent run gets a punishment probe (1), placebo children (3) and an ablation child (1).</li>
          <li>Condition files give the labels. No endpoint accepts a label.</li>
          <li>
            Seeds are in <Code>conditions/seeds.yaml</Code>: 3 pilot, 10 dev, 10 test.
          </li>
          <li>
            <Code>conditions/frozen.json</Code> stores the approver, the time and the file hashes of each stage. The API refuses test seeds unless <Code>GET /freeze</Code> says{' '}
            <Code>valid: true</Code>.
          </li>
          <li>The report gives, per group, the C1 recall and the false-alarm rate with a Clopper-Pearson upper bound.</li>
        </ul>
      </Section>

      <Section title="Usage">
        <p className="text-sm">
          Install with <Code>uv sync</Code> and <Code>pnpm --dir web install</Code>. Host locally with <Code>host.cmd</Code> (Windows) or <Code>./host.sh</Code>. The CLI is a client of the API. Add{' '}
          <Code>--json</Code> to print the API body.
        </p>
        <pre className="overflow-x-auto rounded-lg border bg-muted p-3 text-xs">{CLI_EXAMPLES}</pre>
        <p className="text-xs text-muted-foreground">
          Paid calls stay off unless the server runs with <Code>SIM_ALLOW_PAID=1</Code>. Run <Code>uv run collusion-sim --help</Code> for all groups.
        </p>
      </Section>

      <Section title="API">
        <p className="text-sm">
          Contract: <RepoLink path="docs/sim/api/openapi.yaml" />. Guide and change log: <RepoLink path="docs/sim/api/README.md" />. Errors use <Code>application/problem+json</Code> with a stable{' '}
          <Code>code</Code>. The WebSocket <Code>{'/api/ws/runs/{run_id}?from_seq=N'}</Code> sends stored events, then <Code>caught_up</Code>, then live events.
        </p>
        <DataTable head={['Group', 'Method and path', 'Purpose']} rows={API_GROUPS.map(([g, p, d]) => [g, <Code key="p">{p}</Code>, d])} />
      </Section>
    </div>
  )
}
