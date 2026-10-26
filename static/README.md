# static: the web UI without a server

A read-only copy of the platform's web UI with the recorded data of the three experiments (100 episodes: dev2 50, dev2 PC-plus 10, test 40). The workbench replays any run round by round: prices, chat, the metrics panel and the request and reply of every model call (Trail).

## Open it

A browser runs the recorded API inside a service worker, which needs an http address (a double click on `index.html` does not work). Python 3 with the standard library only:

```
cd static/site
python serve.py 8080       # then open http://127.0.0.1:8080
```

Start pages: `/runs` (all runs), `/experiments` (the three experiments), `/workbench/<run_id>` (one run). Example: `/workbench/run_01M5SM65KTEM2YK0NFN1H4QSBD` is test PC-plus seed 311.

## What is inside

| Path | Content |
|---|---|
| `site/index.html`, `site/assets/`, `site/theme-init.js`, `site/favicon.svg` | The built UI |
| `site/mockServiceWorker.js` | The service worker: it answers every `/api` request from the recorded files |
| `site/snapshot/index.json` | The shared API answers (health, scenarios, conditions, seeds, freeze, the run and experiment lists, the experiment feature tables, the evaluations) |
| `site/snapshot/runs/<run_id>.json.gz` | One run: the run record, its indicators, every event (as the live WebSocket sends them) and the trail of every model call |
| `site/serve.py` | A small static server that sends `index.html` for any page address, so a deep link also loads after a reload |

## Limits

- Read only. An action that changes data (start a run, archive, clone) answers 405 "This is a read-only snapshot".
- A request that was not recorded answers 404 "not in this snapshot". The provider profiles and the file browser were not recorded: they hold local settings, not study data.
- The answers were recorded with GET requests from the study server (API 0.45.0) on 26 Oct 2026, so they equal what the live platform showed for these runs. The recorder is `platform/web/scripts/record-snapshot.mjs`, and the handlers are in `platform/web/src/mocks/snapshot.ts`.
