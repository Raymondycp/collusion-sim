import { useEffect, useState, type ReactNode } from 'react'
import { Code, DataTable, RepoLink, Section } from './Flow'

type JudgeProgress = { job: string; where: string; state: string; valid: number; target: number; tries: number; invalid: number; cap: number; cost_usd: number; updated_utc: string }

// The server's judge run writes this file every 30 s (web/public/status/, not in git); with no run there is no file.
const PROGRESS_URL = '/status/judge-progress.json'

function JudgeProgressBox() {
  const [p, setP] = useState<JudgeProgress | null>(null)
  useEffect(() => {
    let live = true
    const load = () =>
      fetch(PROGRESS_URL, { cache: 'no-store' })
        .then((r) => (r.ok ? r.json() : null))
        .then((j: JudgeProgress | null) => live && setP(j))
        .catch(() => live && setP(null))
    load()
    const id = setInterval(load, 30_000)
    return () => {
      live = false
      clearInterval(id)
    }
  }, [])
  if (!p) return null
  const share = Math.min(100, Math.round((100 * p.valid) / p.target))
  return (
    <section className="space-y-2 rounded-lg border p-4 text-sm" data-testid="judge-progress">
      <h2 className="font-medium">Job progress: {p.job}</h2>
      <div className="h-2 w-full overflow-hidden rounded bg-muted" role="progressbar" aria-valuenow={p.valid} aria-valuemin={0} aria-valuemax={p.target} aria-label="Valid judge replies">
        <div className="h-full bg-foreground/70" style={{ width: `${share}%` }} />
      </div>
      <p>
        {p.state}: {p.valid} of {p.target} valid replies ({share}%). {p.tries} tries, {p.invalid} invalid, cap {p.cap}. Cost so far about {p.cost_usd.toFixed(2)} USD (from token counts and the list
        price). Runs on the {p.where}. Updated {p.updated_utc} UTC; this box reloads every 30 s.
      </p>
    </section>
  )
}

const DATA = 'paper/data/haiku-elastic/'
const DEV = DATA + 'dev/'

function F({ dir, name }: { dir: string; name: string }) {
  return <RepoLink path={dir + name}>{<code className="text-xs">{name}</code>}</RepoLink>
}

const DESIGN: [string, string, string, string, string, string][] = [
  ['C1', 'The pricing prompt allows coordination with the other seller', 'Private', 'yes', 'yes', 'required'],
  ['C1-public-private', 'As C1, but a public board and a private chat are both open; the agent chooses', 'Public and private', 'yes', 'yes', 'extension'],
  ['C2a', 'Base prompt, no channel', 'None', 'no', 'no', 'required'],
  ['C3-accept', 'The C1 talk prompt, but the pricing call cannot see the channel', 'Private', 'yes', 'no', 'required'],
  ['C4', 'No channel; the pricing prompt asks for the highest total profit over the long run', 'None', 'undetermined', 'yes', 'extension'],
]

const SEEDS: [string, string, string][] = [
  ['301', '27', 'Public'],
  ['302', '27', 'Public'],
  ['303', '35', 'Public'],
  ['304', '28', 'Public'],
  ['305', '26', 'Public'],
  ['306', '33', 'Only A'],
  ['307', '34', 'Only B'],
  ['308', '28', 'Only A'],
  ['309', '35', 'Only B'],
  ['310', '31', 'Only A'],
]

const RAW: [ReactNode, string, string, string][] = [
  [<F key="zip" dir={DEV} name="experiment.zip" />, 'See the next table', 'All raw records of the experiment (8 MB zipped, about 118 MB unzipped, 152 files)', 'Main source for the team member’s measures'],
  [
    <F key="f" dir={DEV} name="features.csv" />,
    'One parent run (50 rows)',
    'Labels, seed, hashes and the built-in features delta_p, br_gap_net, msg_volume, corr_raw, corr_lag, corr_cond, timeout_fallbacks, degraded_calls, each with <id>_missing',
    'Reference and cross-check only',
  ],
  [<F key="m" dir={DEV} name="features.csv.meta.json" />, 'The whole table', 'meta (feature definitions, versions, code hash), columns, rows', 'Look up the definition of a column'],
  [<F key="r" dir={DEV} name="runs-check.csv" />, 'One parent run (50 rows)', 'The lead’s quality check: end, rounds, llm_calls, call_not_ok, acted, fallback, posts_private, posts_public', 'Check the quality'],
]

const ZIP: [string, string, string][] = [
  ['experiment.json', 'The whole experiment', 'Conditions, seeds, budget, spend, run ID and status of each cell'],
  ['manifest.json', 'The whole export', 'Hash of each file'],
  ['runs/<run_id>/meta.json', 'One run', 'Condition, seed, labels, status, end reason, rounds, calls, cache hits, fallbacks, demand parameters'],
  ['runs/<run_id>/episode.jsonl', 'One event', 'The first line is a header (condition, labels, channels, limits, demand, models). Each later line is one event (next table)'],
  ['runs/<run_id>/trail.jsonl', 'One LLM call', 'The full messages sent, tools, reply, thinking parts, tokens, latency, error'],
]

const EVENTS: [string, string, string][] = [
  ['round_started', 'Start of a round', 'round, reference (Nash and joint-optimum prices of the round)'],
  ['news_published', 'One shock message', 'text, visible (who sees it)'],
  ['observed', 'The data one seller receives', 'agent_id, purpose (talk/act), observation'],
  ['llm_call', 'One LLM call', 'purpose, attempt, cache_hit, error, tokens, latency'],
  ['talk_replied', 'One talk reply of a seller', 'channel_id, outcome (posted/pass/error), text'],
  ['posted', 'One message sent', 'agent_id, channel_id (private_A_B or public), text, receivers'],
  ['planned', 'The pricing plan of a seller', 'plan, source'],
  ['acted', 'The pricing action of a seller', 'price, asked_price, hold, fallback, forced, tool_calls, limits_hit'],
  ['settled', 'Settlement of a round', 'prices, quantities, profits, clamped'],
  ['reference', 'Reference values of a round', 'reference, best_response, consumer_surplus'],
  ['run_ended', 'End of the run', 'status, end_reason, episode_hash'],
]

const MEASURES: [string, string][] = [
  ['Channel C (private talk or not)', 'posted events with channel_id = private_A_B. Count the messages that each run actually sent, not the group'],
  ['Memory M (refers to earlier talk)', 'text of posted and talk_replied, and the replies in trail.jsonl; needs a judge'],
  ['Talk J (degree of coordination talk)', 'text of posted; needs a judge'],
  ['Dependence', 'acted prices against the rival price of the last round; best_response in reference'],
  ['Coalition advantage CA', 'profits and quantities in settled; Nash and joint-optimum values in reference'],
]

const QUALITY: [string, string, string, string, string, string, string][] = [
  ['C1', '10', '1,222', '608', '0', '603', '0'],
  ['C1-public-private', '10', '1,231', '608', '0', '128', '472'],
  ['C2a', '10', '608', '608', '0', '0', '0'],
  ['C3-accept', '10', '1,219', '608', '0', '604', '0'],
  ['C4', '10', '608', '608', '0', '0', '0'],
]

export function HaikuData() {
  return (
    <>
      <JudgeProgressBox />
      <section className="space-y-2">
        <p className="text-sm text-muted-foreground">
          A second study that follows a team member’s design: Netflix against Disney+ in an elastic subscription market, with Claude Haiku 5.5 sellers. Source:{' '}
          <RepoLink path={DATA + 'README.md'} />. Design and decisions: <RepoLink path="docs/sim/DECISIONS-35.md" />. The team member’s spec: <RepoLink path={DATA + 'SPEC.md'} />. Do not mix
          these data with the main study.
        </p>
      </section>

      <section className="space-y-1 rounded-lg border border-dashed p-4 text-sm" data-testid="haiku-read-first">
        <h2 className="font-medium">Read first</h2>
        <p>
          These are dev data only (seeds 301-310). There is no test split, so a threshold or rule is chosen and evaluated on the same data. Each run is a parent run with no child runs, so{' '}
          <Code>punishment_depth</Code> and the <Code>ablation_*</Code> columns are empty. All 50 runs passed the quality check (section 6).
        </p>
      </section>

      <Section title="1. Folders">
        <pre className="overflow-x-auto rounded-lg border bg-muted p-3 text-xs">{`paper/data/haiku-elastic/
  README.md   data index (this page)
  SPEC.md     the team member's spec (rules #1-#7, case table)
  dev/        seeds 301-310, experiment exp_01M5QY9834PPBPRQDANQGHTS9R (no platform judge)
studies/haiku-elastic/
  conditions/ frozen conditions, prompts, models, seed map (frozen.json holds every hash)
  scenarios/  the market scenario of each seed (demand, shocks, stop round)
paper/analysis-elastic/
  analysis.ipynb, build_notebook.py   the elastic study notebook
  out/dev/                            notebook outputs`}</pre>
        <p className="text-sm">5 conditions x 10 seeds = 50 parent runs.</p>
      </Section>

      <Section title="2. Design">
        <DataTable head={['Condition', 'Design (the one change)', 'Channel', 'label_legal', 'label_harm', 'Tag']} rows={DESIGN.map(([c, ...rest]) => [<Code key={c}>{c}</Code>, ...rest])} />
        <ul className="list-disc space-y-1 pl-5 text-sm">
          <li>Seller A is Netflix and seller B is Disney+. Both use claude-haiku-5-5. models.yaml says temperature 0.7, but this model does not accept a temperature (the API says it is deprecated), and the platform did not send it: settings is {} in trail.jsonl for all 4,888 calls of the pilot (and for all 6,101 dev2 and 4,173 test calls). So the sellers used the model's default sampling.</li>
          <li>
            Market: q = a - b·p + d·(rival price), with a=10, b=3.0, d=2.5, c=1. Symmetric Nash price 3.714, joint optimum 10.5, price grid 0-15. The shocks of each seed move these values;
            the value of each round is in the <Code>reference</Code> event.
          </li>
          <li>Groups with a channel talk and then price (2 calls per seller per round). Groups with no channel only price (1 call). At most 1 message per round.</li>
          <li>A seed gives the same market and shocks in every condition, so groups pair by seed.</li>
        </ul>
        <DataTable head={['Seed', 'Rounds', 'Shock news']} rows={SEEDS} />
      </Section>

      <Section title="3. Raw data: paper/data/haiku-elastic/dev/">
        <p className="text-sm">The platform exports these files, except runs-check.csv. They are not processed.</p>
        <DataTable head={['File', 'One row is', 'Contents', 'Use']} rows={RAW} />
        <h3 className="pt-2 text-sm font-medium">Layout of experiment.zip</h3>
        <DataTable head={['Path', 'One row is', 'Contents']} rows={ZIP.map(([p, r, c]) => [<Code key={p}>{p}</Code>, r, c])} />
        <h3 className="pt-2 text-sm font-medium">Events in episode.jsonl</h3>
        <DataTable head={['type', 'One row is', 'Main fields']} rows={EVENTS.map(([t, r, c]) => [<Code key={t}>{t}</Code>, r, c])} />
      </Section>

      <Section title="4. Where each measure of the team member comes from">
        <p className="text-sm">The notebook computes the measures from episode.jsonl. This table is a guide; the notebook states each definition and choice.</p>
        <DataTable head={['Measure', 'Main source']} rows={MEASURES} />
      </Section>

      <Section title="5. Notebook outputs: paper/analysis-elastic/out/dev/">
        <p className="text-sm">
          The elastic researcher builds these. Each new table or figure is listed in <RepoLink path={DATA + 'README.md'} /> section 5.
        </p>
      </Section>

      <Section title="6. Quality check (lead, 25 Oct)">
        <ul className="list-disc space-y-1 pl-5 text-sm">
          <li>All 50 pilot runs reached the horizon. 4,888 LLM calls in total.</li>
          <li>The agents made all 3,040 price actions: 0 fallbacks, 0 forced actions. 667 of them are holds that the agent chose. They are not errors.</li>
          <li>24 calls failed (20 parse errors, 3 timeouts, 1 provider error). A retry recovered each one, and no action was affected.</li>
          <li>
            C1-public-private allows 1 message per round. 176 actions hit this limit (the agent tried to send more in the same round), so the agent picks one channel per round.
          </li>
          <li>
            C3-accept uses the C1 talk prompt, so its round-1 talk request (also round 2 for seed 310) is the same as in C1, and the call cache gave the C1 reply (22 cache hits). These
            messages are not independent samples.
          </li>
          <li>With two sellers, the public board and the private chat have the same readers. A move to the private chat does not mean that the agent avoids an observer.</li>
        </ul>
        <DataTable head={['Condition', 'Runs', 'LLM calls', 'Price actions', 'Fallbacks', 'Private messages', 'Public messages']} rows={QUALITY} />
      </Section>

      <Section title="7. Shared columns">
        <DataTable
          head={['Column', 'Values']}
          rows={[
            ['condition_id', 'C1, C1-public-private, C2a, C3-accept, C4 (section 2)'],
            ['label_legal / label_harm', 'Set by the condition design, not by outcomes (section 2). C4 is undetermined under the legal standard'],
            ['seed', '301-310. A seed gives the same market and shocks in every condition'],
            ['t', 'Round number, from 1'],
            ['agent_id / seller', 'A (Netflix) or B (Disney+)'],
            ['channel_id', 'private_A_B (private chat) or public (public board)'],
          ].map(([c, v]) => [<Code key={c}>{c}</Code>, v])}
        />
      </Section>
    </>
  )
}
