"""Writes web/src/mocks/researchFixtures.ts from real backend answers (GET /schema/run-config, /conditions, /seeds, /freeze)."""
import json, sys, urllib.request

base = sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:8000/api'
get = lambda p: json.load(urllib.request.urlopen(f'{base}/{p}'))
health = get('health')
out = [
    f"// Copies of real backend answers (api {health['api_version']}, committed backend). Regenerate with `uv run --no-project python scripts/make_research_fixtures.py` (from web/, backend on :8000); do not edit by hand.",
    "import type { Condition, SeedSplits } from '@/api/runConfig'",
    "import type { components } from '@/api/schema'",
    "import type { JSONSchema } from '@/lib/jsonSchema'",
    '',
    f"export const realRunConfigSchema = {json.dumps(get('schema/run-config'), indent=2)} as JSONSchema",
    '',
    f"export const realConditions = {json.dumps(get('conditions'), indent=2)} satisfies Condition[]",
    '',
    f"export const realSeeds = {json.dumps(get('seeds'))} satisfies SeedSplits",
    '',
    f"export const realFreeze = {json.dumps(get('freeze'))} satisfies components['schemas']['FreezeStatus']",
    '',
]
open('src/mocks/researchFixtures.ts', 'w', encoding='utf-8', newline='\n').write('\n'.join(out))
print('written')
