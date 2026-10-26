"""Judge reads (item 3 unit J1): the item set file, the registries and the blinded transcript of a parent run."""

from __future__ import annotations

from collections.abc import Callable

import yaml

from adapters.store import RunsDB
from judge import items as itemsets
from judge.blocks import BLOCKS
from judge.transcript import run_view
from judge.views import VIEWS, NotJudged, Rendered
from services.configs import ApiError, RunConfig
from services.research import RESEARCH_FILES, Research


class JudgeReads:
    def __init__(self, db: RunsDB, research: Research, trail_of: Callable[[str], list[dict]] | None = None) -> None:
        """trail_of: run id -> its trail lines, for the view talk_thinking (DECISIONS-14 §3.2)."""
        self.db, self.research, self.trail_of = db, research, trail_of

    def views(self) -> list[dict]:
        return [v.body() for v in VIEWS.values()]

    def blocks(self) -> list[dict]:
        return [b.body() for b in BLOCKS.values()]

    def item_sets(self) -> list[dict]:
        """The one item set, from conditions/judge-rubric.yaml (empty list when the file is missing). An invalid file is
        listed with valid false and its problems, so the Settings editor can show them."""
        stored = self.research.stored_file(RESEARCH_FILES["judge_rubric"])
        if stored["text"] is None:
            return []
        try:
            data = yaml.safe_load(stored["text"])
        except yaml.YAMLError:
            data = None
        item_set, problems = itemsets.parse(data)
        frozen = self.research.freeze_status()["stages"]["conditions"]
        body = {
            "file": stored["name"],
            "hash": stored["hash"],
            # As the research_frozen write check (CR-31 m2); GET /freeze gives the stage's `valid`
            "status": "frozen" if frozen["frozen"] else "draft",
            "frozen_at": frozen["frozen_at"] if frozen["frozen"] else None,
            "valid": item_set is not None,
            "problems": problems,
        }
        if item_set is None:
            return [body | {"id": None, "items": None, "paraphrases": None, "jev_version": None, "quote_min_words": None}]
        items = item_set.items
        return [
            body
            | {
                "id": item_set.id,
                "items": [
                    {"id": "agreement", "text": items.agreement.text},
                    {"id": "offer", "text": items.offer.text},
                    {"id": "acceptance", "text": items.acceptance.text},
                    {"id": "talk_score", "text": items.talk_score.text, "anchors": list(items.talk_score.anchors)},
                ],
                "guard": item_set.guard,
                "quote_min_words": item_set.quote_min_words,
                "paraphrases": {
                    b: [{"index": i + 1, "hash": item_set.paraphrase_hash(b, i)} for i in range(itemsets.N_PARAPHRASES)]
                    for b in ("llm", "jev")
                },
                "jev_version": item_set.jev.model,
            }
        ]

    def rendered(self, run_id: str, view: str, transform: str) -> Rendered:
        if self.db.run_config(run_id) is None:
            raise ApiError(404, "not_found", f"Run {run_id} not found.")
        try:
            cfg = RunConfig.model_validate_json(self.db.run_config(run_id)["config_json"])
            trail = self.trail_of(run_id) if view == "talk_thinking" and self.trail_of else None
            return run_view(view, run_id, self.db.events(run_id), cfg, transform, trail)
        except NotJudged as e:
            raise ApiError(422, "validation_error", f"Run {run_id}: {e}.", [{"path": "run_id", "message": str(e)}]) from None

    def transcript(self, run_id: str, view: str, transform: str) -> dict:
        """The blinded view as a backend would get it. No condition, seed, seller id or channel id (FE plan §3.2)."""
        return {"run_id": run_id} | self.rendered(run_id, view, transform).body()
