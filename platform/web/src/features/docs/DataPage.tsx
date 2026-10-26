import type { ReactNode } from 'react'
import { Link, useSearchParams } from 'react-router'
import { Code, DataTable, RepoLink, Section } from './Flow'
import { HaikuData } from './HaikuData'

const DATA = 'paper/data/'
const TABLES = 'paper/analysis/out/dev/tables/'
const OUT = 'paper/analysis/out/dev/'

/** A file name that links to the repo. Links open the dev copy unless the file exists only in test. */
function F({ dir, name }: { dir: string; name: string }) {
  return <RepoLink path={dir + name}>{<code className="text-xs">{name}</code>}</RepoLink>
}

const rows = (dir: string, items: [string, ...ReactNode[]][]) => items.map(([name, ...rest]) => [<F key={name} dir={dir} name={name} />, ...rest])

const RAW = rows(DATA + 'dev/', [
  ['features.csv', 'One parent run (30 rows)', 'Label, seed, child run IDs, all features, quality columns, judge columns', 'Main table for rule and feature analysis'],
  ['features.csv.meta.json', 'The whole table', 'Feature definitions, feature code hash, judge job data', 'Look up the definition and version of a column'],
  ['judge.json', 'One item in episodes = one parent run', 'Judge verdict on the talk: votes for 3 wordings, agreement (0/1), talk score, quotes, number of dropped quotes', 'Talk evidence'],
  ['experiment.zip', 'See the next table', 'All raw records of the experiment (about 148 MB unzipped)', 'Use only when you need round-level or call-level detail'],
  ['rule-fit.json', 'One candidate rule (dev only)', 'Thresholds of 9 rules fitted on dev, hits per group, missing counts, CP upper bounds; selected is the chosen rule', 'Rule fit'],
])

const RAW_TEST = rows(DATA + 'test/', [
  ['rule-apply.json', 'One item per group (test only)', 'Hits of the frozen rule on test. It is not a result', 'Cross-check'],
  ['lead-scan-429.txt', 'One run (test only)', 'Fallback count and 429 count of each run', 'Check the quality table'],
])

const ZIP: [string, string, string][] = [
  ['experiment.json', 'The whole experiment', 'Conditions, seeds, child run plan, budget, run ID and status of each cell'],
  ['runs/<run_id>/meta.json', 'One run', 'Summary of the run: condition, seed, label, status, end reason, call count, fallback count'],
  ['runs/<run_id>/episode.jsonl', 'One event', 'The first line is a header. Each later line is one event; type is, for example, round_started, observed, llm_call, talk_replied, posted, acted, settled, reference, run_ended'],
  ['runs/<run_id>/trail.jsonl', 'One LLM call', 'Full request messages, tools, reply, tokens, latency, error code'],
]

const TIDY = rows(TABLES, [
  ['runs.csv', 'One run (180 rows)', 'run_id, kind (parent/probe/placebo/ablation), condition_id, seed, status, end_reason, fork_t, llm_calls, err_fallback, talk_errors, calls_429, broken'],
  ['rounds.csv', 'One seller in one round of one run', 't, seller, price, asked_price, profit, hold, fallback_reason, clamped, nash, joint, br (best response to the rival price of the last round), cost (unit cost of the seller), a_, b_, d_ (demand parameters of the round)'],
  ['dp_rounds.csv', 'One round of one run', 'Mean price of the two sellers, nash, joint, dp (how far above Nash), quiet (inside the quiet window)'],
  ['talk.csv', 'One talk reply', 't, seller, outcome (posted/pass/error), text, channel_id, action_format (action format only), chars'],
  ['calls.csv', 'One LLM call', 'purpose (talk/act), attempt, cache_hit, error, http_status, wall_time, the settings sent'],
  ['episodes.csv', 'One parent run (30 rows)', 'Same as features.csv, kept for the notebook merge'],
  ['judge_episodes.csv', 'One parent run', 'j_agreement, j_talk_score, j_n_ok, j_split_vote (votes differ), j_forced (forced to 0), j_dropped'],
  ['judge_calls.csv', 'One judge call', 'paraphrase (wording), status, dropped, error_reason'],
])

const CHECKS = rows(TABLES, [
  ['A0_inventory.csv', 'One input file', 'Whether the file exists, its size, SHA-256'],
  ['A7_design_intent.csv', 'One condition', 'Whether the design intent held: runs and rounds with dp > 0; probe raised or cut the price'],
  ['A7_probe_direction.csv', 'One parent run', 'Price of seller B in round 20 against the Nash price'],
  ['A7_cache_by_kind.csv', 'Condition x run kind x call kind', 'Number of calls and cache hits'],
  ['A7b_errors_by_run.csv', 'One run', 'Fallbacks, talk errors, 429 count, time of the first 429'],
  ['A7b_quality_by_group_kind.csv', 'Condition x run kind', 'Number of broken runs, error share'],
  ['A7b_feature_validity.csv', 'One condition', 'How many parent runs have sound input for each feature'],
  ['A7b_calls_after_first_429.csv (test only)', 'One row', 'Number and result of calls sent after the first 429'],
  ['manipulation_checks.csv', 'One condition', 'Manipulation check: finished runs, noop share, timeout fallbacks, judge problem counts, clamped prices'],
])

const OUTPUTS: [ReactNode, ReactNode][] = [
  [<F key="c1" dir={OUT} name="C1_single_feature_auc.csv" />, 'AUC and confidence interval of each feature against each negative group'],
  [<F key="c2" dir={OUT} name="C2_threshold_sweep.csv" />, 'C1 recall and per-group false-alarm rate of each feature at each threshold (descriptive)'],
  [<><F dir={OUT} name="C3_rule_fits_dev.csv" /> (dev)</>, 'Fit of the 9 candidate rules; same as rule-fit.json'],
  [<><F dir="paper/analysis/out/test/" name="C5_rule_apply.csv" /> (test)</>, 'Hits of the frozen rule on test, and how many runs had sound input'],
  [<F key="c6" dir={OUT} name="C6_ceiling.csv" />, 'AUC and recall of the logistic regression ceiling (reference limit only)'],
  [<><F dir={OUT} name="C7_rule_basis.csv" />, <F dir={OUT} name="C7_values.csv" /></>, 'The two input values and the verdict of the frozen rule R7 for each run'],
  [<F key="c8" dir={OUT} name="C8_valid_parent_features.csv" />, 'Feature statistics from sound parent runs only'],
  [<><F dir="paper/analysis/out/test/" name="C8_judge_broken_parents.csv" /> (test)</>, 'Judge results for broken parent runs'],
  [<F key="b2" dir={OUT} name="B2_talk_by_condition.csv" />, 'Number of post types in each condition'],
  [<>E* (all)</>, 'Exploratory views (part E). They are not pre-specified analyses. No rule is applied to test in them'],
  [<F key="e1" dir={OUT} name="E1_rule_basis.png" />, 'Scatter plot of the two inputs of R7 (dev)'],
  [<><F dir={OUT} name="E2_loso_selection.csv" />, <F dir={OUT} name="E2_loso_summary.csv" /></>, 'Leave one seed out (dev): each fold drops one seed, refits and selects a rule (one row per fold); summary of hits on the dropped runs. The threshold of each rule is in its own column. R9 has only t2, so t1 is empty in the R9 folds'],
  [<F key="e3" dir={OUT} name="E3_frontier.csv" />, 'Best recall of each rule at several false-alarm limits (dev)'],
  [<><F dir={OUT} name="E4_feature_corr_C1.csv" /> (one per condition), <F dir={OUT} name="E4_corr_heatmap.html" /></>, 'Correlation matrix between features'],
  [<F key="e5" dir={OUT} name="E5_seed_pairs.csv" />, 'C1 against the negative groups on the same seed'],
  [<F key="e6" dir={OUT} name="E6_episode_feature_z.csv" />, 'Standard score (z) of each feature for each parent run'],
  [<><F dir={OUT} name="E7_error_matrix.csv" />, <F dir={OUT} name="E7_error_timeline.png" />, <F dir={OUT} name="E7_error_by_round.png" />, <F dir="paper/analysis/out/test/" name="E7_timeline_summary.csv" /> (test)</>, 'Errors per run and round (rows are runs, columns are rounds). The timeline plots each call against UTC time; the by-round view plots it against the round. Both splits have it: dev shows no errors (a contrast); test shows when the 429 failures came'],
  [<><F dir={OUT} name="F1_dp_round_median.csv" />, <F dir={OUT} name="F1_dp_paths.png" /></>, 'Median dp of each condition in each round (one row is one round of one condition), and the dp path of each run. Quiet-window rounds are included'],
  [<><F dir={OUT} name="F2_points.csv" />, <F dir={OUT} name="F2_best_response.png" /></>, 'Best-response view: one row is one seller in one round of one run, with the price, the rival price of the last round, the best response, whether the price is above it, and whether the run is broken'],
  [<><F dir={OUT} name="F3_case_matrix.csv" />, <F dir={OUT} name="F3_case_matrix.png" />, <F dir={OUT} name="F3_case_matrix.md" /> (dev; the .md is Table CT in the paper)</>, 'Three case types x three evidence types: one row is one condition, with the design, the labels, the design intent and the dev measurement; *_by_design marks the cells that the design sets'],
  [<>B*.png, E*.png, *.html</>, 'Figures: price paths, talk, feature distributions, probe response, correlation heat map'],
  [<><F dir={OUT} name="D_report_lines.md" />, <F dir="paper/analysis/out/" name="report.md" /></>, 'Text reports'],
  [<F key="nb" dir="paper/analysis/out/test/" name="analysis-test-executed.ipynb" />, 'The notebook executed on test data'],
]

const COLUMNS: [string, ReactNode][] = [
  ['condition_id', 'C1 (private chat, agreement possible), C2a (no channel), C3-accept (C1 talk, but pricing cannot see the channel)'],
  ['gkey', 'Short name of the condition, used for merges'],
  ['label_legal / label_harm', 'Set by the condition design: C1 yes/yes, C2a no/no, C3-accept yes/no'],
  ['kind', 'parent, probe, placebo, ablation'],
  ['seed', '101-110 (dev) or 201-210 (test). A seed gives the same market shocks in every condition, so groups pair by seed'],
  ['t', 'Round number, from 1'],
  ['seller / agent_id', 'A or B'],
  ['<column>_missing', 'Why the value is empty, for example no_talk (no talk)'],
  ['run_id, parent_run_id', 'Run ID; a child run points to its parent in parent_run_id'],
]

const STUDIES = [
  { id: 'main', name: 'Main study (Luna)' },
  { id: 'haiku-elastic', name: 'Haiku elastic study' },
] as const

export function DataPage() {
  const [params] = useSearchParams()
  const study = params.get('study') === 'haiku-elastic' ? 'haiku-elastic' : 'main'
  return (
    <div className="mx-auto max-w-4xl space-y-8">
      <section className="space-y-2">
        <h1 className="text-xl font-semibold">Data structure</h1>
        <nav aria-label="Study" className="flex gap-2 text-sm">
          {STUDIES.map((s) => (
            <Link
              key={s.id}
              to={s.id === 'main' ? '?' : `?study=${s.id}`}
              aria-current={study === s.id ? 'page' : undefined}
              className={`rounded-md border px-3 py-1 ${study === s.id ? 'bg-muted font-medium' : 'text-muted-foreground'}`}
            >
              {s.name}
            </Link>
          ))}
        </nav>
      </section>
      {study === 'haiku-elastic' ? <HaikuData /> : <MainData />}
    </div>
  )
}

function MainData() {
  return (
    <>
      <section className="space-y-2">
        <p className="text-sm text-muted-foreground">
          What each data file holds, what one row is, and what the file is for. Source: <RepoLink path="paper/data/README.md" />. Findings: <RepoLink path="paper/drafts/paper.md" />. Feature
          definitions: <RepoLink path="src/features/README.md" />. Links open the dev copy; the test folders use the same file names.
        </p>
      </section>

      <section className="space-y-1 rounded-lg border border-dashed p-4 text-sm" data-testid="read-first">
        <h2 className="font-medium">Read first</h2>
        <p>
          From 11:30:25 UTC the test split hit the provider quota (HTTP 429). Most child runs hold only fallbacks (the price stays the same). Use the <Code>broken</Code> column in{' '}
          <Code>runs.csv</Code> or <Code>A7b_errors_by_run.csv</Code> to see which runs are usable. The frozen rule cannot be measured on test. <Code>rule-apply.json</Code> is not a result.
        </p>
      </section>

      <Section title="1. Folders">
        <pre className="overflow-x-auto rounded-lg border bg-muted p-3 text-xs">{`paper/data/
  dev/   seeds 101-110, experiment exp_01M5PHANRFMT2SRSWYRY0E0VH2, judge eval_01M5PMEAVKG56MY8QNF1P81K7H
  test/  seeds 201-210, experiment exp_01M5PRAJR5ZEFTJMGJ1B9KXMST, judge eval_01M5PV0ZBRZW4GH47AKQWJ0SYJ
paper/analysis/out/
  dev/   figures and tables the notebook makes from dev (tables/ holds the tidy long tables)
  test/  the same, from test; also the executed notebook
  report.md  merged text report of the notebook`}</pre>
        <p className="text-sm">
          Each split has 3 conditions x 10 seeds = 30 parent runs. Each parent run has 5 child runs: 1 probe, 3 placebo, 1 ablation. So each split has 180 runs.
        </p>
      </Section>

      <Section title="2. Raw data: paper/data/<split>/">
        <p className="text-sm">The platform exports these files. They are not processed.</p>
        <DataTable head={['File', 'One row is', 'Contents', 'Use']} rows={[...RAW, ...RAW_TEST]} />
        <h3 className="pt-2 text-sm font-medium">Layout of experiment.zip</h3>
        <DataTable head={['Path', 'One row is', 'Contents']} rows={ZIP.map(([p, r, c]) => [<Code key={p}>{p}</Code>, r, c])} />
      </Section>

      <Section title="3. Tidy long tables: paper/analysis/out/<split>/tables/">
        <p className="text-sm">The notebook builds these from experiment.zip and judge.json. Use them most often.</p>
        <DataTable head={['File', 'One row is', 'Main columns']} rows={TIDY} />
      </Section>

      <Section title="4. Check tables: paper/analysis/out/<split>/tables/">
        <DataTable head={['File', 'One row is', 'Contents']} rows={CHECKS} />
      </Section>

      <Section title="5. Analysis outputs: paper/analysis/out/<split>/">
        <DataTable head={['File', 'Contents']} rows={OUTPUTS.map(([f, c]) => [f, c])} />
      </Section>

      <Section title="6. Shared columns">
        <DataTable head={['Column', 'Values']} rows={COLUMNS.map(([c, v]) => [<Code key={c}>{c}</Code>, v])} />
      </Section>

      <Section title="7. How to rerun the notebook">
        <pre className="overflow-x-auto rounded-lg border bg-muted p-3 text-xs">
          {'uv run --no-project --with jupyter --with nbconvert --with pandas --with matplotlib --with plotly --with scipy --with pyyaml jupyter nbconvert --to notebook --execute --inplace paper/analysis/analysis.ipynb'}
        </pre>
        <p className="text-sm">
          The notebook reads dev by default. Set the environment variable <Code>ANALYSIS_SPLIT=test</Code> to read test.
        </p>
      </Section>
    </>
  )
}
