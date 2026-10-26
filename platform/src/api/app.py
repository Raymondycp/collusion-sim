"""FastAPI app under /api. Thin routes over services/. /api/docs shows the contract (docs/sim/api/openapi.yaml)."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import typing
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Callable, Literal

import yaml
from fastapi import Body, FastAPI, Query, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException as StarletteHTTPException

import indicators
from adapters import durable
from adapters.keys import Backend, KeyStore
from adapters.llm.cache import CallCache
from adapters.server_lock import DataFolderInUse, ServerLock
from adapters.store import RunsDB, Writer
from adapters.trail import run_trail
from api import ws
from features.table import to_csv
from judge.store import EvalDB
from judge.views import ViewId
from services import archive as archive_svc
from services import features as features_svc
from services import files as files_svc
from services import imports as imports_svc
from services import profiles as profiles_svc
from services import read
from services import scenarios as scenarios_svc
from services.catalog import Catalog, Paths
from services.configs import ApiError, BudgetSettings, FieldError, RunConfig
from services.evaluations import EvaluationCreate, EvaluationService
from services.experiments import ExperimentCreate, ExperimentService
from services.imports import ImportCreate
from services.judging import JudgeReads
from services.llm_runs import LlmRuns, uses_llm
from services.profiles import ProfileService, paid_calls_allowed
from services.research import RESEARCH_FILES, Research
from services.run_schema import run_config_schema
from services.runner import RunService, dbos_path, new_id
from simcore import __version__
from simcore.branch import BranchRequest
from simcore.demand import DEMAND
from simcore.generators import GENERATORS
from simcore.roles import ROLES
from simcore.scripted import SCRIPTED
from simcore.tools import TOOLS
from simschema.events import SCHEMA_VERSION

log = logging.getLogger("sim.api")
CONTRACT = Path(__file__).parents[2] / "docs" / "sim" / "api" / "openapi.yaml"
SPEC = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
API_VERSION = SPEC["info"]["version"]
SWAGGER = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.17.14"
NOT_BUILT = (
    ("get", "/budget"),
    ("post", "/scenarios/{scenario_id}/generate-news"),
    ("post", "/prompts/preview"),
    ("get", "/presets"),
    ("post", "/presets"),
    ("get", "/presets/{preset_id}"),
    ("post", "/runs/{run_id}/clone"),
    ("get", "/runs/compare"),
)


def problem(status: int, code: str, message: str, errors: list[dict] | None = None) -> JSONResponse:
    body = {
        "type": f"https://collusion-sim.local/problems/{code}",
        "title": code.replace("_", " "),
        "status": status,
        "code": code,
        "message": message,
    }
    if errors:
        body["errors"] = errors
    return JSONResponse(body, status_code=status, media_type="application/problem+json")


class GenerateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    generator_id: str
    inputs: dict[str, Any]
    seed: int = 0
    provider_profile_id: str | None = None
    budget: BudgetSettings | None = None


DEFAULTS_FILE = "defaults.json"
"""PUT /defaults values, in the data root (DECISIONS-11 §1)."""


class TextBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=200_000)


class FreezeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved_by: str = Field(min_length=1, max_length=80)


class ResearchFreezeBody(FreezeBody):
    """POST /freeze only: which stage of the research freeze (DECISIONS-11 §2.5)."""

    stage: Literal["conditions", "rule"]


def estimate_usd_max(config: RunConfig, n_runs: int) -> float | None:
    """Upper cost bound of a batch: the per-run cap times the runs, 0 without LLM agents, None without a USD cap."""
    if not uses_llm(config):
        return 0.0
    return None if config.budget.max_usd is None else round(config.budget.max_usd * n_runs, 6)


class BranchBody(BranchRequest):
    budget: BudgetSettings | None = None


def _active_profiles(db: RunsDB) -> set[str]:
    out = set()
    for row in db.runs():
        if (row["status"] or "queued") in ("queued", "running", "paused"):
            for a in json.loads(row["config_json"]).get("agents", []):
                llm = (a.get("policy") or {}).get("llm") or {}
                if llm.get("provider_profile_id"):
                    out.add(llm["provider_profile_id"])
    return out


def _error_path(err: dict) -> str:
    """The contract path of a validation error. A FieldError from a model validator names its field under the model's path."""
    loc = [str(x) for x in err["loc"] if x != "body"]
    cause = (err.get("ctx") or {}).get("error")
    return "/".join(loc + [cause.field] if isinstance(cause, FieldError) else loc)


def _policy_types() -> list[dict]:
    out = [{"kind": "llm", "name": "LLM agent", "roles": ["seller"], "params": []}]
    for kind, cls in SCRIPTED.items():
        params = []
        hints = typing.get_type_hints(cls.__init__)
        for name, p in list(inspect.signature(cls.__init__).parameters.items())[2:]:
            schema = TypeAdapter(hints[name]).json_schema()
            entry = {"name": name, "type": schema.get("type", "string"), "schema": schema}
            if p.default is not inspect.Parameter.empty:
                entry["default"] = p.default
            params.append(entry)
        out.append({"kind": kind, "name": (cls.__doc__ or kind).strip().splitlines()[0][:80], "roles": ["seller"], "params": params})
    out.append({"kind": "scripted_host", "name": "Scripted host (frozen news)", "roles": ["host"], "params": []})
    return out


def _zip_response(f: typing.IO[bytes], name: str) -> StreamingResponse:
    """Stream a spooled zip in chunks and close it after the last one."""

    def chunks():
        with f:
            while chunk := f.read(1 << 16):
                yield chunk

    return StreamingResponse(
        chunks(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}.zip"'},
        background=BackgroundTask(f.close),  # also when the client leaves before the first chunk (CR-66 addendum 2)
    )


def create_app(
    root: Path | None = None,
    use_dbos: bool | None = None,
    key_backend: Backend | None = None,
    llm_transport: Callable[[], Any] | None = None,
) -> FastAPI:
    """llm_transport: tests only (a fake endpoint). Without it, LLM runs need SIM_ALLOW_PAID=1."""
    paths = Paths(Path(root or os.environ.get("SIM_ROOT", Path.cwd())))
    use_dbos = os.environ.get("SIM_DURABLE", "0") == "1" if use_dbos is None else use_dbos

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        lock = ServerLock(paths.data)
        try:
            lock.acquire()
        except DataFolderInUse as e:
            log.error("%s", e)
            raise
        try:
            async with _services(app):
                yield
        finally:
            lock.release()

    @asynccontextmanager
    async def _services(app: FastAPI):
        db = RunsDB(paths.data / "runs.sqlite")
        writer = Writer(db)
        catalog = Catalog(paths)
        profiles = ProfileService(paths.data / "profiles.sqlite", KeyStore(paths.root / ".env", backend=key_backend))
        paths.cache.mkdir(parents=True, exist_ok=True)
        llm = LlmRuns(profiles, CallCache(paths.cache / "calls.sqlite"), transport=llm_transport, history=db.profile_calls)
        research = Research(paths.conditions, catalog, profiles.record)
        svc = RunService(
            db, writer, catalog, paths.runs, use_dbos=use_dbos, llm=llm, research=research, cache_path=paths.cache / "calls.sqlite"
        )
        experiments = ExperimentService(
            db, writer, svc, research, profiles, paths.experiments
        )  # sets the run hooks before DBOS recovers runs
        if use_dbos:
            await asyncio.to_thread(durable.launch, dbos_path(paths.data), svc.execute)
        await asyncio.to_thread(experiments.recover)
        app.state.db, app.state.svc, app.state.paths, app.state.catalog, app.state.profiles = db, svc, paths, catalog, profiles
        app.state.research, app.state.experiments = research, experiments

        def trail_of(run_id: str) -> list[dict]:
            return run_trail(paths.runs, paths.cache / "calls.sqlite", run_id, db.events(run_id))

        app.state.judge, app.state.trail_of = JudgeReads(db, research, trail_of), trail_of
        eval_db = EvalDB(paths.data / "eval.sqlite")
        eval_writer = Writer(eval_db)
        evaluations = EvaluationService(db, eval_db, eval_writer, research, app.state.judge, new_id, llm)
        evaluations.recover()
        app.state.evaluations = evaluations
        try:
            yield
        finally:
            evaluations.close()
            eval_writer.close()
            experiments.close()
            svc.close()
            if use_dbos:
                durable.shutdown()
            writer.close()
            catalog.close()
            profiles.close()
            llm.close()

    app = FastAPI(title="Collusion simulator API", version=API_VERSION, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, e: ApiError):
        return problem(e.status, e.code, e.message, e.errors)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, e: StarletteHTTPException):
        if e.status_code == 404:
            return problem(404, "not_found", f"No such path: {request.url.path}")
        if e.status_code == 405:
            return problem(405, "method_not_allowed", f"{request.method} is not allowed on {request.url.path}")
        return problem(e.status_code, "http_error", str(e.detail))

    def not_built(request: Request):
        raise ApiError(
            501, "not_built", f"{request.method} {request.url.path} is in the contract but not built yet (docs/sim/api/README.md)."
        )

    for method, path in NOT_BUILT:
        app.add_api_route(f"/api{path}", not_built, methods=[method.upper()], include_in_schema=False)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, e: RequestValidationError):
        errors = [{"path": _error_path(err), "message": err["msg"]} for err in e.errors()]
        return problem(422, "validation_error", "The request does not match the contract.", errors)

    @app.get("/api/openapi.json", include_in_schema=False)
    def contract():
        return JSONResponse(SPEC)

    @app.get("/api/docs", include_in_schema=False)
    def docs():
        return get_swagger_ui_html(
            openapi_url="/api/openapi.json",
            title="Collusion simulator API (contract)",
            swagger_favicon_url="data:,",
            swagger_js_url=f"{SWAGGER}/swagger-ui-bundle.js",
            swagger_css_url=f"{SWAGGER}/swagger-ui.css",
        )

    # System and registries

    @app.get("/api/health")
    def health():
        return {
            "status": "ok",
            "api_version": API_VERSION,
            "simcore_version": __version__,
            "schema_version": SCHEMA_VERSION,
            "durable_backend": "dbos" if use_dbos else "none",
            "paid_calls_enabled": paid_calls_allowed(paths.root / ".env"),
            "freeze": {"frozen": False, "frozen_at": None},
        }

    @app.get("/api/roles")
    def roles():
        return [r.model_dump(mode="json") for r in ROLES.specs()]

    @app.get("/api/tools")
    def tools():
        return [t.model_dump(mode="json") for t in TOOLS.specs()]

    @app.get("/api/policies")
    def policies():
        return _policy_types()

    @app.get("/api/schema/run-config")
    def run_config_json_schema():
        return run_config_schema(_policy_types())

    @app.get("/api/indicators")
    def indicator_specs():
        return indicators.REGISTRY.specs()

    @app.get("/api/demand-models")
    def demand_models():
        return [spec.info() for spec in DEMAND.specs()]

    @app.post("/api/demand-models/{model_id}/solve")
    def solve_demand(model_id: str, targets: dict[str, Any] = Body(...)):
        return scenarios_svc.solve(model_id, targets)

    @app.get("/api/scenarios")
    def scenarios(request: Request, status: str | None = None):
        return [s for s in request.app.state.catalog.summaries() if status in (None, s["status"])]

    @app.post("/api/scenarios", status_code=201)
    def create_scenario(request: Request, body: dict[str, Any] = Body(...)):
        return request.app.state.catalog.create(new_id("scn_"), body)

    @app.put("/api/scenarios/{scenario_id}")
    def replace_scenario(request: Request, scenario_id: str, body: dict[str, Any] = Body(...)):
        return request.app.state.catalog.replace(scenario_id, body)

    @app.delete("/api/scenarios/{scenario_id}", status_code=204)
    def delete_scenario(request: Request, scenario_id: str):
        request.app.state.catalog.delete(scenario_id)
        return Response(status_code=204)

    @app.post("/api/scenarios/{scenario_id}/check")
    def check_scenario(request: Request, scenario_id: str):
        return request.app.state.catalog.any(scenario_id).check().model_dump(mode="json")

    @app.post("/api/scenarios/{scenario_id}/freeze")
    def freeze_scenario(request: Request, scenario_id: str, body: FreezeBody):
        return request.app.state.catalog.freeze(scenario_id, body.approved_by)

    @app.post("/api/market/preview")
    def market_preview(body: dict[str, Any] = Body(...)):
        return scenarios_svc.market_preview(body)

    @app.get("/api/scenario-generators")
    def scenario_generators():
        return [g.info() for g in GENERATORS.specs()]

    @app.post("/api/scenarios/generate")
    def generate_scenario(body: GenerateBody):
        return scenarios_svc.generate(body.generator_id, body.inputs, body.seed)

    @app.get("/api/scenarios/{scenario_id}/reference")
    def scenario_reference(request: Request, scenario_id: str, until: Annotated[int | None, Query(ge=1)] = None):
        return scenarios_svc.reference_series(request.app.state.catalog.any(scenario_id), until)

    @app.get("/api/scenarios/{scenario_id}")
    def scenario(request: Request, scenario_id: str):
        return request.app.state.catalog.record(scenario_id)

    # Provider profiles (keys stay server side; responses carry key_set, key_hint, key_store, key_env only)

    def with_window(request: Request, view: dict) -> dict:
        return view | {"rate_window": request.app.state.svc.llm.window_status(view["id"])}

    @app.get("/api/defaults")
    def defaults():
        return profiles_svc.defaults(paths.root / ".env", paths.data / DEFAULTS_FILE)

    @app.put("/api/defaults")
    def put_defaults(request: Request, body: profiles_svc.DefaultsBody):
        profiles_svc.save_defaults(paths.data / DEFAULTS_FILE, body, request.app.state.profiles)
        return profiles_svc.defaults(paths.root / ".env", paths.data / DEFAULTS_FILE)

    @app.get("/api/provider-profiles")
    def list_profiles(request: Request):
        return [with_window(request, p) for p in request.app.state.profiles.list()]

    @app.post("/api/provider-profiles", status_code=201)
    def create_profile(request: Request, body: dict[str, Any] = Body(...)):
        return request.app.state.profiles.create(new_id("prof_"), body)

    @app.get("/api/provider-profiles/{profile_id}")
    def get_profile(request: Request, profile_id: str):
        return with_window(request, request.app.state.profiles.get(profile_id))

    @app.patch("/api/provider-profiles/{profile_id}")
    def update_profile(request: Request, profile_id: str, body: dict[str, Any] = Body(...)):
        return request.app.state.profiles.update(profile_id, body)

    @app.delete("/api/provider-profiles/{profile_id}", status_code=204)
    def delete_profile(request: Request, profile_id: str):
        request.app.state.profiles.delete(profile_id, in_use=profile_id in _active_profiles(request.app.state.db))
        return Response(status_code=204)

    @app.post("/api/provider-profiles/{profile_id}/test")
    def test_profile(request: Request, profile_id: str, body: dict[str, Any] | None = Body(default=None)):
        request.app.state.profiles.record(profile_id)
        if not paid_calls_allowed(request.app.state.paths.root / ".env"):
            raise ApiError(
                409,
                "paid_calls_disabled",
                "A test call is a paid LLM call. The server runs with paid calls off (set SIM_ALLOW_PAID=1 after lead approval).",
            )
        body = body or {}
        thinking = body.get("thinking")
        if thinking is not None and not (isinstance(thinking, bool) or thinking in ("minimal", "low", "medium", "high", "xhigh")):
            raise ApiError(
                422,
                "validation_error",
                "thinking must be true, false, minimal, low, medium, high or xhigh.",
                [{"path": "thinking", "message": "invalid value"}],
            )
        transport = llm_transport() if llm_transport else None  # the fake endpoint in tests and scripts/fake_llm_server.py
        return profiles_svc.probe_profile(request.app.state.profiles, profile_id, body.get("model"), transport, thinking=thinking)

    @app.get("/api/provider-profiles/{profile_id}/models")
    def profile_models(request: Request, profile_id: str):
        return request.app.state.profiles.models(profile_id, transport=llm_transport() if llm_transport else None)

    # Runs

    @app.get("/api/runs")
    def list_runs(
        request: Request,
        kind: str | None = None,
        status: str | None = None,
        condition_id: str | None = None,
        split: str | None = None,
        parent_run_id: str | None = None,
        batch_id: str | None = None,
        scenario_id: str | None = None,
        preset_id: str | None = None,
        model: str | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
        include_archived: bool = False,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        cursor: str | None = None,
    ):
        db = request.app.state.db
        held, archived = db.held(), db.archived("run")
        items = [read.with_hold(read.summary(r), held) | {"archived_at": archived.get(r["run_id"])} for r in db.runs()]
        if not include_archived:
            items = [i for i in items if i["archived_at"] is None]
        want = {
            "kind": kind,
            "status": status,
            "condition_id": condition_id,
            "split": split,
            "batch_id": batch_id,
            "scenario_id": scenario_id,
            "preset_id": preset_id,
        }
        items = [i for i in items if all(v is None or i.get(k) == v for k, v in want.items())]
        if parent_run_id is not None:
            items = [i for i in items if i["branch"] and i["branch"]["parent_run_id"] == parent_run_id]
        if model is not None:
            items = [i for i in items if model in i["models"]]
        if created_from:
            items = [i for i in items if i["created_at"][:10] >= created_from]
        if created_to:
            items = [i for i in items if i["created_at"][:10] <= created_to]
        start = int(cursor) if cursor and cursor.isdigit() else 0
        page = items[start : start + limit]
        return {"items": page, "next_cursor": str(start + limit) if start + limit < len(items) else None}

    @app.get("/api/conditions")
    def list_conditions(request: Request):
        return [c.body() for c in request.app.state.research.conditions()]

    @app.get("/api/conditions/{condition_id}")
    def get_condition(request: Request, condition_id: str):
        return request.app.state.research.condition(condition_id).body()

    @app.get("/api/conditions/{condition_id}/file")
    def get_condition_file(request: Request, condition_id: str):
        return request.app.state.research.condition_file(condition_id)

    @app.put("/api/conditions/{condition_id}/file")
    def put_condition_file(request: Request, condition_id: str, body: TextBody):
        return request.app.state.research.save_condition_file(condition_id, body.text)

    @app.get("/api/research/{name}")
    def get_research_file(request: Request, name: Literal["decision-rule", "judge-rubric", "models"]):
        return request.app.state.research.stored_file(RESEARCH_FILES[name.replace("-", "_")])

    @app.put("/api/research/{name}")
    def put_research_file(request: Request, name: Literal["decision-rule", "judge-rubric", "models"], body: TextBody):
        s = request.app.state
        return s.research.save_research_file(name.replace("-", "_"), body.text, s.db.test_cell_count())

    @app.get("/api/judge/views")
    def judge_views(request: Request):
        return request.app.state.judge.views()

    @app.get("/api/judge/blocks")
    def judge_blocks(request: Request):
        return request.app.state.judge.blocks()

    @app.get("/api/judge/item-sets")
    def judge_item_sets(request: Request):
        return request.app.state.judge.item_sets()

    @app.get("/api/judge/ledger")
    def judge_ledger(request: Request):
        return request.app.state.evaluations.ledger()

    @app.post("/api/evaluations/estimate")
    def estimate_evaluation(request: Request, body: EvaluationCreate):
        return request.app.state.evaluations.estimate(body)

    @app.post("/api/evaluations", status_code=202)
    def create_evaluation(request: Request, body: EvaluationCreate):
        return request.app.state.evaluations.create(body)

    @app.get("/api/evaluations")
    def list_evaluations(
        request: Request,
        run_id: str | None = None,
        block: Literal["wording", "main", "support_one_wording", "talk_prices", "stability", "robustness"] | None = None,
        status: Literal["queued", "running", "completed", "stopped", "failed"] | None = None,
    ):
        return request.app.state.evaluations.list(run_id, block, status)

    @app.get("/api/evaluations/{evaluation_id}")
    def get_evaluation(request: Request, evaluation_id: str):
        return request.app.state.evaluations.detail(evaluation_id)

    @app.post("/api/evaluations/{evaluation_id}/resume", status_code=202)
    def resume_evaluation(request: Request, evaluation_id: str):
        return request.app.state.evaluations.resume(evaluation_id)

    @app.get("/api/runs/{run_id}/transcript")
    def run_transcript(request: Request, run_id: str, view: ViewId, transform: Literal["none", "round_shuffle"] = "none"):
        return request.app.state.judge.transcript(run_id, view, transform)

    @app.get("/api/seeds")
    def get_seeds(request: Request):
        return request.app.state.research.seeds_body()

    @app.get("/api/freeze")
    def get_freeze(request: Request):
        research = request.app.state.research
        return research.freeze_status() | {"inputs": research.freeze_inputs()}

    @app.post("/api/freeze")
    def post_freeze(request: Request, body: ResearchFreezeBody):
        s = request.app.state
        return s.research.freeze(body.stage, body.approved_by, s.db.test_cell_count())

    @app.post("/api/experiments/estimate")
    def estimate_experiment(request: Request, body: ExperimentCreate):
        return request.app.state.experiments.estimate(body)

    @app.post("/api/experiments", status_code=202)
    def create_experiment(request: Request, body: ExperimentCreate):
        return request.app.state.experiments.create(body)

    @app.get("/api/experiments")
    def list_experiments(request: Request, include_archived: bool = False):
        archived = request.app.state.db.archived("experiment")
        items = [e | {"archived_at": archived.get(e["id"])} for e in request.app.state.experiments.summaries()]
        return items if include_archived else [e for e in items if e["archived_at"] is None]

    @app.get("/api/experiments/{experiment_id}")
    def get_experiment(request: Request, experiment_id: str):
        detail = request.app.state.experiments.detail(experiment_id)
        return detail | {"archived_at": request.app.state.db.archived("experiment").get(experiment_id)}

    def archive_route(kind: archive_svc.Kind, path: str, archive: bool):
        def route(request: Request, body: archive_svc.ArchiveRequest):
            s = request.app.state
            if kind == "run":
                status = {r["run_id"]: r["status"] for r in s.db.runs()}
            else:
                status = {e["id"]: e["status"] for e in s.experiments.summaries()}
            return archive_svc.change(s.db, s.svc.writer, kind, status, body, archive, s.svc.now())

        app.post(f"/api/{path}/{'archive' if archive else 'unarchive'}")(route)

    for _kind, _path in (("run", "runs"), ("experiment", "experiments")):
        for _archive in (True, False):
            archive_route(_kind, _path, _archive)

    @app.post("/api/experiments/{experiment_id}/retry")
    def retry_experiment(request: Request, experiment_id: str):
        return request.app.state.experiments.retry(experiment_id)

    @app.get("/api/experiments/{experiment_id}/export")
    def export_experiment(request: Request, experiment_id: str, format: Literal["zip"]):
        s = request.app.state
        detail = s.experiments.detail(experiment_id)
        if detail["split"] == "test":  # the same gate as the feature export of a test experiment
            s.research.check_test_open()
        return _zip_response(read.experiment_zip(s.db, paths.runs, paths.cache / "calls.sqlite", detail), experiment_id)

    @app.get("/api/experiments/{experiment_id}/features")
    def experiment_features(
        request: Request,
        experiment_id: str,
        layer: Literal["A", "B"] = "B",
        format: Literal["json", "csv"] = "json",
        evaluation_id: Annotated[list[str] | None, Query()] = None,
    ):
        s = request.app.state
        evaluations = [s.evaluations.detail(e) for e in dict.fromkeys(evaluation_id or [])]
        meta, columns, rows = features_svc.table(s.db, s.research.catalog, s.research, experiment_id, layer, evaluations, s.trail_of)
        if format == "csv":
            return PlainTextResponse(to_csv(columns, rows), media_type="text/csv")
        return {"meta": meta, "columns": columns, "rows": rows}

    @app.post("/api/runs/estimate")
    def estimate_runs(request: Request, config: RunConfig):
        return request.app.state.svc.estimate(config)

    @app.post("/api/runs", status_code=202)
    def start_runs(request: Request, config: RunConfig):
        batch_id, run_ids = request.app.state.svc.start(config)
        return {"batch_id": batch_id, "run_ids": run_ids, "estimate_usd_max": estimate_usd_max(config, len(run_ids))}

    @app.post("/api/imports", status_code=201)
    def create_import(request: Request, response: Response, body: ImportCreate):
        s = request.app.state
        created, out = imports_svc.create(s.db, s.svc.writer, s.paths.runs, body, s.svc.now(), s.svc.cache_path)
        if not created:
            response.status_code = 200
        return out

    @app.get("/api/runs/{run_id}")
    def get_run(request: Request, run_id: str):
        db = request.app.state.db
        return read.with_hold(read.detail(db, run_id), db.held()) | {"archived_at": db.archived("run").get(run_id)}

    def control_route(action: str):
        def control(request: Request, run_id: str, body: dict[str, Any] | None = Body(default=None)):
            request.app.state.svc.control(run_id, action)
            return read.summary(read.find(request.app.state.db, run_id))

        return control

    for action in ("pause", "resume", "stop"):
        app.post(f"/api/runs/{{run_id}}/{action}", name=f"{action}Run")(control_route(action))

    @app.get("/api/runs/{run_id}/branches")
    def list_branches(request: Request, run_id: str):
        read.find(request.app.state.db, run_id)
        return [read.summary(r) for r in request.app.state.db.runs() if r["parent_run_id"] == run_id]

    @app.post("/api/runs/{run_id}/branches", status_code=202)
    def create_branches(request: Request, run_id: str, body: BranchBody):
        batch_id, ids = request.app.state.svc.branch(run_id, BranchRequest.model_validate(body.model_dump(exclude={"budget"})), body.budget)
        config = RunConfig.model_validate_json(request.app.state.db.run_config(ids[0])["config_json"])
        return {"batch_id": batch_id, "run_ids": ids, "estimate_usd_max": estimate_usd_max(config, len(ids))}

    @app.get("/api/runs/{run_id}/events")
    def run_events(
        request: Request,
        run_id: str,
        from_seq: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=5000)] = 1000,
        types: str | None = None,
        agent_id: str | None = None,
    ):
        read.find(request.app.state.db, run_id)
        return read.event_page(request.app.state.db, run_id, from_seq, limit, set(types.split(",")) if types else None, agent_id)

    @app.get("/api/runs/{run_id}/series")
    def run_series(request: Request, run_id: str):
        read.find(request.app.state.db, run_id)
        return read.series(run_id, request.app.state.db.events(run_id))

    @app.get("/api/runs/{run_id}/state")
    def run_state(request: Request, run_id: str, t: Annotated[int | None, Query(ge=0)] = None):
        return read.folded_state(request.app.state.db, run_id, t)

    @app.get("/api/runs/{run_id}/observations")
    def run_observation(request: Request, run_id: str, agent_id: str, t: int):
        return read.observation(request.app.state.db, paths.cache / "calls.sqlite", run_id, agent_id, t)

    @app.get("/api/runs/{run_id}/indicators/{indicator_id}")
    def run_indicator(
        request: Request, run_id: str, indicator_id: str, params: str | None = None, until: Annotated[int | None, Query(ge=1)] = None
    ):
        if indicator_id not in indicators.REGISTRY:
            raise ApiError(404, "unknown_indicator", f"Indicator {indicator_id} is not registered.")
        read.find(request.app.state.db, run_id)
        try:
            p = json.loads(params) if params else {}
        except json.JSONDecodeError:
            raise ApiError(
                422, "indicator_params_invalid", "params is not valid JSON.", [{"path": "params", "message": "invalid JSON"}]
            ) from None
        view = indicators.SeriesView.from_events(request.app.state.db.events(run_id))
        try:
            return {"run_id": run_id, **read.indicator_body(indicators.instance(view, indicator_id, p, until))}
        except indicators.ParamsError as e:
            raise ApiError(422, "indicator_params_invalid", str(e), e.errors) from None

    @app.get("/api/runs/{run_id}/episode.jsonl")
    def episode(request: Request, run_id: str):
        return PlainTextResponse(
            read.episode_text(request.app.state.db, run_id),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": f'attachment; filename="{run_id}.episode.jsonl"'},
        )

    @app.get("/api/runs/{run_id}/calls/{seq}/trail")
    def call_trail(request: Request, run_id: str, seq: int):
        s = request.app.state
        if read.run_split(read.find(s.db, run_id)) == "test":  # the same gate as the run zip, which holds this line
            s.research.check_test_open()
        return read.trail_line(s.db, paths.runs, paths.cache / "calls.sqlite", run_id, seq)

    @app.get("/api/runs/{run_id}/export")
    def export(request: Request, run_id: str, format: str, include: str | None = None):
        db = request.app.state.db
        if format == "zip":
            if read.run_split(read.find(db, run_id)) == "test":  # the gate of the experiment and feature exports (CR-66)
                request.app.state.research.check_test_open()
            return _zip_response(read.export_zip(db, paths.runs, paths.cache / "calls.sqlite", run_id, include == "branches"), run_id)
        if format == "csv_rounds":
            read.find(db, run_id)
            return PlainTextResponse(read.export_csv(run_id, db.events(run_id)), media_type="text/csv")
        raise ApiError(
            422,
            "validation_error",
            f"Export format {format} is not built (use zip or csv_rounds).",
            [{"path": "format", "message": "not built"}],
        )

    # Read-only files for the web file browser (DECISIONS-33)

    @app.get("/api/files/tree")
    def files_tree():
        return files_svc.tree(paths.root.resolve())

    @app.get("/api/files/content")
    def files_content(path: str):
        body, media = files_svc.read_file(paths.root.resolve(), path)
        return Response(body, media_type=media, headers=files_svc.SAFE_HEADERS)

    @app.get("/api/files/zip")
    def files_zip(path: str, member: str | None = None):
        base = paths.root.resolve()
        if member is None:
            return files_svc.zip_members(base, path)
        return PlainTextResponse(files_svc.zip_member_text(base, path, member), headers=files_svc.SAFE_HEADERS)

    # Ledger

    @app.get("/api/ledger")
    def ledger(request: Request, limit: Annotated[int, Query(ge=1, le=1000)] = 200, cursor: str | None = None):
        rows = [read.ledger_entry(r) for r in request.app.state.db.ledger()]
        start = int(cursor) if cursor and cursor.isdigit() else 0
        return {"items": rows[start : start + limit], "next_cursor": str(start + limit) if start + limit < len(rows) else None}

    @app.get("/api/ledger/verify")
    def ledger_verify(request: Request):
        ok, bad = request.app.state.db.verify_ledger()
        return {"ok": ok, "entries": len(request.app.state.db.ledger()), "first_bad_line": bad}

    @app.websocket("/api/ws/runs/{run_id}")
    async def run_socket(websocket: WebSocket, run_id: str, from_seq: int = 0):
        await ws.stream(websocket, websocket.app.state.db, run_id, from_seq)

    return app
