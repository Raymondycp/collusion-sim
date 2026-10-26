"""Read-only file access for the web file browser (DECISIONS-33): an allowlist of folders under SIM_ROOT, nothing else."""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

from services.configs import ApiError

ROOTS = ("paper/drafts", "paper/analysis", "paper/data", "paper/review", "conditions")
MAX_BYTES = 20_000_000
SKIP_DIRS = {"__pycache__", "node_modules"}
SAFE_HEADERS = {"Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'", "X-Content-Type-Options": "nosniff"}
"""A served file never runs as a page of this origin, even when its URL is opened directly (an .svg can hold a script)."""

IMAGES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".svg": "image/svg+xml"}
TEXT = {
    ".json": "application/json",
    ".ipynb": "application/json",
    ".jsonl": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".tsv": "text/plain",
    ".html": "text/plain",  # never text/html: the client decides how to show a page
    ".htm": "text/plain",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".py": "text/plain",
    ".yaml": "text/plain",
    ".yml": "text/plain",
    ".toml": "text/plain",
}


def _not_found(rel: str) -> ApiError:
    return ApiError(404, "not_found", f"No such file: {rel[:200]}")


def _hidden(parts: tuple[str, ...] | list[str]) -> bool:
    return any(p.startswith(".") or p in SKIP_DIRS for p in parts)


def _root_of(base: Path, real: Path) -> Path | None:
    for r in ROOTS:
        root = (base / r).resolve()
        if real == root or real.is_relative_to(root):
            return root
    return None


def resolve(base: Path, rel: str) -> Path:
    """The real path of an allowed file or folder. Anything else is a 404: `..`, an absolute path, a dot segment, a symlink out of the root."""
    if not rel or "\\" in rel or "\x00" in rel or ":" in rel or rel.startswith("/"):
        raise _not_found(rel)
    parts = rel.split("/")
    if any(p in ("", ".", "..") or p.startswith(".") for p in parts):
        raise _not_found(rel)
    if not any(rel == r or rel.startswith(r + "/") for r in ROOTS):
        raise _not_found(rel)
    real = (base / rel).resolve()
    root = _root_of(base, real)
    if root is None or _hidden(real.relative_to(root).parts) or not real.exists():  # a symlink may point at a dot file inside the root
        raise _not_found(rel)
    return real


def tree(base: Path) -> dict:
    out = []
    for r in ROOTS:
        top = base / r
        if not top.is_dir():
            continue
        real_top = top.resolve()
        entries: list[dict] = []
        for dirpath, dirs, files in os.walk(top):
            dirs[:] = sorted(d for d in dirs if not _hidden([d]) and not (Path(dirpath) / d).is_symlink())
            here = Path(dirpath)
            for d in dirs:
                entries.append({"path": (here / d).relative_to(base).as_posix(), "kind": "dir", "size": 0})
            for f in sorted(files):
                p = here / f
                if _hidden([f]):
                    continue
                real = p.resolve()
                if not real.is_file() or not real.is_relative_to(real_top) or _hidden(real.relative_to(real_top).parts):
                    continue
                entries.append({"path": p.relative_to(base).as_posix(), "kind": "file", "size": real.stat().st_size})
        out.append({"path": r, "entries": entries})
    return {"roots": out}


def read_file(base: Path, rel: str) -> tuple[bytes, str]:
    p = resolve(base, rel)
    if not p.is_file():
        raise _not_found(rel)
    ext = p.suffix.lower()
    media = IMAGES.get(ext) or TEXT.get(ext)
    if media is None:
        raise ApiError(415, "file_not_viewable", f"A {ext or 'no-extension'} file cannot be shown here.")
    if p.stat().st_size > MAX_BYTES:
        raise ApiError(413, "file_too_large", f"The file is larger than {MAX_BYTES // 1_000_000} MB.")
    return p.read_bytes(), media


def _open_zip(base: Path, rel: str) -> tuple[Path, zipfile.ZipFile]:
    p = resolve(base, rel)
    if not p.is_file():
        raise _not_found(rel)
    if p.suffix.lower() != ".zip":
        raise ApiError(415, "file_not_viewable", "This route reads .zip files only.")
    try:
        return p, zipfile.ZipFile(p)
    except zipfile.BadZipFile:
        raise ApiError(415, "file_not_viewable", "The file is not a valid zip.") from None


def zip_members(base: Path, rel: str) -> dict:
    _, z = _open_zip(base, rel)
    with z:
        return {"members": [{"name": i.filename, "size": i.file_size} for i in z.infolist() if not i.is_dir()]}


def zip_member_text(base: Path, rel: str, member: str) -> str:
    _, z = _open_zip(base, rel)
    with z:
        try:
            info = z.getinfo(member)
        except KeyError:
            raise _not_found(member) from None
        if info.is_dir():
            raise _not_found(member)
        too_big = ApiError(413, "file_too_large", f"The member is larger than {MAX_BYTES // 1_000_000} MB uncompressed.")
        if info.file_size > MAX_BYTES:
            raise too_big
        with z.open(info) as f:
            data = f.read(MAX_BYTES + 1)  # the header size can lie; the read cannot
        if len(data) > MAX_BYTES:
            raise too_big
    return data.decode("utf-8", errors="replace")
