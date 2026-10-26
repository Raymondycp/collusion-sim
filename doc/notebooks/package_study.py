"""Assemble the Haiku elastic study into one clean folder with five parts, for a new repository.

    platform/        the experiment platform code (backend, CLI, web UI source, tests) and the frozen study configuration; no run data
    raw-data/        the experiment exports as the platform wrote them, and the LLM-judge caches
    processed-data/  the tidy tables the notebooks write (data engineering output, the input of every analysis)
    doc/             the paper drafts, the design notes, the notebooks with their builders, the figures
    static/          a static, read-only snapshot of the web UI (from web/dist-snapshot, when it exists)

Each part has a README.md (guide and data dictionary); the folder READMEs are written by hand in paper/analysis-elastic/package/.
No conversation log, reference note, review or audit file is copied. Figure links of the drafts are rewritten to the new
layout. Every file, and every member of every zip, is scanned for keys (KEY-SCAN-REPORT.txt).

Usage, from the repository root: python paper/analysis-elastic/package_study.py [destination]   (default release/haiku-elastic-study)
The package is built in <destination>.new and replaces <destination> only when it is complete and clean.
"""
import json
import os
import re
import shutil
import time
import subprocess
import sys
import zipfile
from pathlib import Path
from urllib.parse import quote, unquote

ROOT = Path(__file__).resolve().parents[2]
FINAL = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "release" / "haiku-elastic-study"
DEST = FINAL.with_name(FINAL.name + ".new")
A = ROOT / "paper/analysis-elastic"
D = ROOT / "paper/data/haiku-elastic"
TEXTS = A / "package"   # hand-written: README.md, platform.md, raw-data.md, processed-data.md, doc.md, static.md, rerun.py
STATIC = ROOT / "web/dist-snapshot"

# platform: tracked files only (no untracked run output), working-tree content (uncommitted fixes included)
PLATFORM_TRACKED = ["src", "tests", "web", "presets", "scripts", "docs/sim/SPEC.md", "docs/sim/api/openapi.yaml", "docs/sim/api/README.md",
                    "docs/sim/api/examples", "pyproject.toml", "uv.lock", ".python-version", ".env.example", "host.sh", "host.cmd"]
PLATFORM_SKIP = re.compile(r"^(web/public/status/|scripts/oracle-deploy\.sh$)")
STUDY = ["conditions", "scenarios"]   # studies/haiku-elastic/<these> -> platform/studies/haiku-elastic/
EXPORTS = ["dev2", "dev2-pcplus", "test"]   # the pilot export (dev) is old data and is left out
JUDGE = {"judge-cache-dev2": "dev2", "judge-cache-test": "test"}
TABLES = {"dev2": A / "out/dev2/tables", "test": A / "out/test/tables", "overview": A / "out/overview"}
FIGURES = {"dev2": A / "out/dev2", "test": A / "out/test", "example": A / "out/example", "overview": A / "out/overview"}
NOTEBOOKS = ["analysis.ipynb", "analysis-test.ipynb", "example.ipynb", "overview.ipynb", "build_notebook.py", "build_example_notebook.py",
             "build_overview_notebook.py", "run_dev.sh", "dump_outputs.py", "check_draft.py", "make_runs_check.py", "package_study.py"]
DUMPS = {"dev2": A / "out/dev2/dump.txt", "test": A / "out/test/dump.txt", "overview": A / "out/overview/dump.txt"}
DRAFTS = {ROOT / "paper/drafts-elastic/paper-elastic.md": "doc/paper/paper-elastic.md",
          D / "Index - Evidence of AI Agnets Collusion-draft.md": "doc/paper/Index - Evidence of AI Agnets Collusion-draft.md"}
LOGS = {ROOT / "paper/lab-notebook.md": "doc/paper/lab-notebook.md"}   # the dated record of the study (the draft's time line cites it); user folders masked in the copy
HOME = re.compile(r"([A-Za-z]:[\\/]+Users[\\/]+)[^\\/\s`'\"]+", re.I)
DESIGN = ["CONDITIONS.md", "SPEC.md", "conditions-comic.svg", "conditions-comic.py", "Screenshot 2026-10-26 112823.png"]
HAND = {"README.md": "README.md", "platform.md": "platform/README.md", "raw-data.md": "raw-data/README.md", "processed-data.md": "processed-data/README.md",
        "doc.md": "doc/README.md", "static.md": "static/README.md", "rerun.py": "doc/notebooks/rerun.py"}
FORBIDDEN = re.compile(r"(^|/)(CLAUDE\.md|\.claude|AUDIT-|LEAD-E|RESEARCHER-|DECISIONS-|CHANGES-REQUESTED|omp-session|\.env$|_build-|_exec-)")
SECRET = re.compile(
    rb"sk-ant-[A-Za-z0-9_-]{6,}|sk-[A-Za-z0-9]{20,}|key_hint\W{1,4}sk|\"api_key\"\s*:\s*\"[^\"]"
    rb"|x-api-key\W{1,4}[A-Za-z0-9]|Bearer [A-Za-z0-9._-]{10,}|ANTHROPIC_API_KEY\s*=\s*[^\s\"']{8,}")
# Secret-pattern hits reviewed by the lead (26 Oct): fake keys of tests and docs, masked hints, header names with no value.
PLACEHOLDER = re.compile(r"FAKE|EXAMPLE|not shown|…|sk-other-0123456789abcdef|abc\.def-123456789|\"zz9|x-api-key\W{1,4}(x-goog|User-Agent|SDK|or )", re.I)
WORDS = re.compile(rb"sk-ant|api_key|ANTHROPIC|key_hint|key_set|key_store|provider_profile|SITE_PASSWORD", re.I)
MOVED: dict[Path, Path] = {}   # source file -> package file, for the link rewrite


def put(src: Path, rel: str) -> None:
    dst = DEST / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    MOVED[src.resolve()] = dst


def put_tree(src: Path, rel: str, skip: tuple = ()) -> None:
    for f in sorted(src.rglob("*")):
        if f.is_file() and "__pycache__" not in f.parts and f.relative_to(src).parts[0] not in skip:
            put(f, f"{rel}/{f.relative_to(src).as_posix()}")


def build() -> None:
    tracked = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", *PLATFORM_TRACKED], cwd=ROOT, capture_output=True, check=True).stdout.decode().split("\0")
    for rel in filter(None, tracked):
        if not PLATFORM_SKIP.search(rel) and (ROOT / rel).is_file():
            put(ROOT / rel, f"platform/{rel}")
    for s in STUDY:
        put_tree(ROOT / "studies/haiku-elastic" / s, f"platform/studies/haiku-elastic/{s}")
    for e in EXPORTS:
        put_tree(D / e, f"raw-data/exports/{e}")
    for src, name in JUDGE.items():
        put_tree(A / src, f"raw-data/judge-cache/{name}")
    for name, src in TABLES.items():
        for f in sorted([*src.glob("*.csv"), *src.glob("*.json")]):
            put(f, f"processed-data/{name}/{f.name}")
    for name, src in FIGURES.items():
        seen: set[str] = set()
        for f in sorted(src.rglob("*.png")):
            if "explore" in f.parts:
                continue   # the lead's exploratory pass, not cited
            assert f.name not in seen, f"two figures named {f.name} in {src}"
            seen.add(f.name)
            put(f, f"doc/figures/{name}/{f.name}")
    for n in NOTEBOOKS:
        put(A / n, f"doc/notebooks/{n}")
    for name, f in DUMPS.items():
        put(f, f"doc/notebooks/dumps/{name}.txt")
    for d in DESIGN:
        put(D / d, f"doc/design/{d}")
    for src, rel in DRAFTS.items():
        put(src, rel)
    for src, rel in LOGS.items():
        put(src, rel)
        dst = DEST / rel
        dst.write_text(HOME.sub(r"<user>", dst.read_text(encoding="utf-8")), encoding="utf-8")
    if STATIC.is_dir():
        put_tree(STATIC, "static/site", skip=("status",))   # status/ is the dev server's job progress, copied by Vite from web/public; not study data
    for src, rel in HAND.items():
        put(TEXTS / src, rel)


def rewrite_links() -> list[str]:
    """Point every image link of the copied drafts at the copied figure; return the links that have no copy."""
    bad = []
    for src, rel in DRAFTS.items():
        dst = DEST / rel

        def fix(m: re.Match) -> str:
            target = (src.parent / unquote(m.group(2))).resolve()
            if target not in MOVED:
                bad.append(f"{rel} -> {m.group(2)}")
                return m.group(0)
            return f"![{m.group(1)}]({quote(Path(os.path.relpath(MOVED[target], dst.parent)).as_posix())})"

        dst.write_text(re.sub(r"!\[([^]]*)\]\(([^)]+)\)", fix, dst.read_text(encoding="utf-8")), encoding="utf-8")
    return bad


def check_links() -> list[str]:
    bad = []
    for md in DEST.rglob("*.md"):
        for target in re.findall(r"!\[[^]]*\]\(([^)]+)\)", md.read_text(encoding="utf-8", errors="replace")):
            if not target.startswith("http") and not (md.parent / unquote(target)).resolve().is_file():
                bad.append(f"{md.relative_to(DEST).as_posix()} -> {target}")
    return bad


def dictionary() -> str:
    """Table list of processed-data: rows, columns and the notebook cell that writes each table."""
    def cells(nb: Path) -> list[tuple[int, str, str]]:
        cs, out = json.loads(nb.read_text(encoding="utf-8"))["cells"], []
        for kind in ("code", "markdown"):   # code cells first; a table named at run time is found by the markdown cell that names it
            heading = ""
            for i, c in enumerate(cs):
                src = "".join(c["source"])
                if c["cell_type"] == "markdown":
                    h = [l.lstrip("# ").strip() for l in src.splitlines() if l.startswith("#")]
                    heading = h[0] if h else heading
                if c["cell_type"] == kind:
                    out.append((i, heading, src))
        return out

    lines = []
    for name, nb in (("dev2", "analysis.ipynb"), ("test", "analysis-test.ipynb"), ("overview", "overview.ipynb")):
        cs = cells(A / nb)
        lines += [f"### `{name}/` (written by `doc/notebooks/{nb}`)", "", "| Table | Rows | Cell | Section | Columns |", "|---|---|---|---|---|"]
        for f in sorted(TABLES[name].glob("*.csv")):
            with open(f, encoding="utf-8") as fh:
                cols, rows = fh.readline().strip(), sum(1 for _ in fh)
            hit = next(((n, h) for n, h, s in cs if f.name in s or f'"{f.stem}' in s), (None, ""))
            lines.append(f"| `{f.name}` | {rows} | {hit[0] if hit[0] is not None else 'n/a'} | {hit[1][:70]} | {cols.replace(',', ', ')} |")
        lines.append("")
    return "\n".join(lines)


def secrets_from_env() -> list[bytes]:
    """The values of the key, token and password lines of the repository .env, and their first 12 and last 8 characters. Never printed."""
    env = ROOT / ".env"
    vals = []
    for line in env.read_text(encoding="utf-8").splitlines() if env.is_file() else []:
        k, _, v = line.partition("=")
        v = v.strip().strip("\"'")
        if re.search(r"KEY|PASSWORD|TOKEN|SECRET", k) and len(v) >= 16:
            vals += [v.encode(), v[:12].encode(), v[-8:].encode()]
    return vals


def scan_bytes(rel: str, data: bytes, env_vals: list[bytes], out: dict) -> None:
    for m in SECRET.finditer(data):
        hit = data[m.start():m.end() + 24].decode(errors="replace").splitlines()[0]
        if PLACEHOLDER.search(hit):
            out["placeholder"].append(f"{rel}: {hit}")
        else:
            out["secret"].append(f"{rel}: {hit[:10]}...")
    if any(v in data for v in env_vals):
        out["env"].append(f"{rel}: a .env key, token or password value (whole, first 12 or last 8 characters)")
    ctx: dict[str, int] = {}
    for m in WORDS.finditer(data):
        c = re.sub(rb"\d{8,}", b"0", data[max(0, m.start() - 40):m.end() + 40]).decode(errors="replace").replace("\n", " ")
        ctx[c] = ctx.get(c, 0) + 1
    if ctx:
        top = sorted(ctx.items(), key=lambda x: -x[1])[:3]
        out["words"].append(f"{rel}: {sum(ctx.values())} hits; top contexts: " + " | ".join(f"[{n}x] {c}" for c, n in top))


def key_scan() -> dict:
    out: dict = {"secret": [], "placeholder": [], "env": [], "words": []}
    env_vals = secrets_from_env()
    out["n_env"] = len(env_vals) // 3
    for f in sorted(DEST.rglob("*")):
        if not f.is_file() or f.name in ("KEY-SCAN-REPORT.txt", "package_study.py"):
            continue
        rel = f.relative_to(DEST).as_posix()
        if f.suffix == ".zip":
            with zipfile.ZipFile(f) as z:
                for m in z.namelist():
                    scan_bytes(f"{rel}!{m}", z.read(m), env_vals, out)
        else:
            scan_bytes(rel, f.read_bytes(), env_vals, out)
    return out


if __name__ == "__main__":
    shutil.rmtree(DEST, ignore_errors=True)
    build()
    pd_readme = DEST / "processed-data/README.md"
    pd_readme.write_text(pd_readme.read_text(encoding="utf-8").replace("<!-- TABLES -->", dictionary()), encoding="utf-8")
    bad = rewrite_links() + check_links()
    forbidden = [f.relative_to(DEST).as_posix() for f in DEST.rglob("*") if FORBIDDEN.search(f.relative_to(DEST).as_posix())]
    user_ = os.environ.get("USERNAME", "").encode()   # the local user name must not leave this machine (plain files; zip members are platform exports)
    named = [f.relative_to(DEST).as_posix() for f in DEST.rglob("*") if user_ and f.is_file() and f.suffix != ".zip" and user_ in f.read_bytes()]
    print("files with the local user name:", named or "none")
    hits = key_scan()
    files = [f for f in DEST.rglob("*") if f.is_file()]
    report = [f"Key scan of {FINAL.name}: {len(files)} files ({sum(f.stat().st_size for f in files) / 1e6:.1f} MB); every zip member scanned;"
              " doc/notebooks/package_study.py is skipped (it holds the patterns).",
              f"1. Secret patterns that are not a reviewed placeholder (must be 0; first 10 characters shown): {len(hits['secret'])}", *hits["secret"],
              f"1b. Secret patterns that are reviewed placeholders (fake keys in tests and docs, masked hints, header names with no value),"
              f" shown in full: {len(hits['placeholder'])}", *hits["placeholder"],
              f"2. The {hits['n_env']} key, token and password values of the repository .env, whole or first 12 or last 8 characters"
              f" (must be 0; values never printed): {len(hits['env'])}", *hits["env"],
              f"3. Words asked for ({WORDS.pattern.decode()}, any case), with contexts for a reader to judge (a run of 8 or more digits is shown as 0):"
              f" {len(hits['words'])} files", *hits["words"]]
    (DEST / "KEY-SCAN-REPORT.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    print(report[0])
    print(f"secret patterns {len(hits['secret'])} (+{len(hits['placeholder'])} reviewed placeholders), .env values {len(hits['env'])} (of {hits['n_env']}), word files {len(hits['words'])}")
    print("broken figure links:", bad or "none")
    print("forbidden files:", forbidden or "none")
    print("static snapshot:", "included" if STATIC.is_dir() else f"not yet ({STATIC} missing)")
    if bad or forbidden or named or hits["secret"] or hits["env"]:
        sys.exit(f"not moved: the new package stays in {DEST}")
    old = FINAL.with_name(FINAL.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    target = FINAL
    if FINAL.exists():
        try:
            FINAL.rename(old)
        except PermissionError:   # a program holds the old folder open: keep it and publish under a dated name
            target = FINAL.with_name(FINAL.name + "-" + time.strftime("%Y%m%d-%H%M"))
            print(f"the old {FINAL.name} is held open by another program; it is kept unchanged")
    DEST.rename(target)
    shutil.rmtree(old, ignore_errors=True)
    print("package:", target)
