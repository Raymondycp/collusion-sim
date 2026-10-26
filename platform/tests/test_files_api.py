"""Read-only file routes (DECISIONS-33): the allowlist, the content types and the refusals."""

import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from services import files


@pytest.fixture
def root(tmp_path):
    for d in ("paper/drafts", "paper/analysis/out/dev", "paper/data/dev", "conditions", "paper/analysis/__pycache__", "paper/analysis/.ipynb_checkpoints"):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / "paper/drafts/paper.md").write_bytes(b"# Title\n")
    (tmp_path / "paper/analysis/out/dev/p.html").write_text("<html>plot</html>", encoding="utf-8")
    (tmp_path / "paper/analysis/out/dev/f.png").write_bytes(b"\x89PNG\r\n\x1a\nxx")
    (tmp_path / "paper/analysis/__pycache__/x.pyc").write_bytes(b"x")
    (tmp_path / "paper/analysis/.ipynb_checkpoints/n.ipynb").write_text("{}", encoding="utf-8")
    (tmp_path / "paper/analysis/.hidden.json").write_text("{}", encoding="utf-8")
    (tmp_path / "conditions/C1.yaml").write_text("id: C1\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("outside\n", encoding="utf-8")
    with zipfile.ZipFile(tmp_path / "paper/data/dev/experiment.zip", "w") as z:
        z.writestr("experiment.json", '{"id": "exp"}')
        z.writestr("runs/run_A/episode.jsonl", '{"a": 1}\n')
    return tmp_path


@pytest.fixture
def client(root):
    with TestClient(create_app(root, use_dbos=False)) as c:
        yield c


def test_tree_lists_only_the_allowlist_without_dot_or_cache_entries(client):
    body = client.get("/api/files/tree").json()
    assert [r["path"] for r in body["roots"]] == ["paper/drafts", "paper/analysis", "paper/data", "conditions"]
    paths = {e["path"]: e for r in body["roots"] for e in r["entries"]}
    assert paths["paper/drafts/paper.md"] == {"path": "paper/drafts/paper.md", "kind": "file", "size": 8}
    assert paths["paper/analysis/out"]["kind"] == "dir"
    assert "paper/analysis/out/dev/f.png" in paths and "paper/data/dev/experiment.zip" in paths
    assert not any("__pycache__" in p or ".ipynb_checkpoints" in p or ".hidden" in p or ".env" in p or "README" in p for p in paths)


def test_content_gives_text_images_and_html_as_plain_text(client):
    md = client.get("/api/files/content", params={"path": "paper/drafts/paper.md"})
    assert md.status_code == 200 and md.content == b"# Title\n" and md.headers["content-type"].startswith("text/markdown")
    png = client.get("/api/files/content", params={"path": "paper/analysis/out/dev/f.png"})
    assert png.headers["content-type"] == "image/png" and png.content.startswith(b"\x89PNG")
    html = client.get("/api/files/content", params={"path": "paper/analysis/out/dev/p.html"})
    assert html.headers["content-type"].startswith("text/plain") and html.text == "<html>plot</html>"
    assert all("sandbox" in r.headers["content-security-policy"] and r.headers["x-content-type-options"] == "nosniff" for r in (md, png, html))


def test_zip_lists_members_and_reads_one(client):
    p = "paper/data/dev/experiment.zip"
    members = client.get("/api/files/zip", params={"path": p}).json()["members"]
    assert {m["name"] for m in members} == {"experiment.json", "runs/run_A/episode.jsonl"}
    m = client.get("/api/files/zip", params={"path": p, "member": "runs/run_A/episode.jsonl"})
    assert m.status_code == 200 and m.text == '{"a": 1}\n' and "sandbox" in m.headers["content-security-policy"]
    assert client.get("/api/files/zip", params={"path": p, "member": "nope"}).status_code == 404
    assert client.get("/api/files/zip", params={"path": "paper/drafts/paper.md"}).status_code == 415


@pytest.mark.parametrize(
    "path",
    [
        "../.env",
        "paper/drafts/../../.env",
        "paper/drafts/../../README.md",
        "/etc/passwd",
        "C:/Windows/win.ini",
        r"paper\drafts\paper.md",
        ".env",
        "README.md",
        "paper/analysis/.hidden.json",
        "paper/analysis/.ipynb_checkpoints/n.ipynb",
        "paper/analysis/__pycache__/x.pyc",
        "paper/drafts//paper.md",
        "paper/drafts-evil/paper.md",
        "paper",
        "paper/missing.md",
    ],
)
def test_a_path_outside_the_allowlist_is_a_404_problem(client, path):
    r = client.get("/api/files/content", params={"path": path})
    assert r.status_code == 404 and r.json()["code"] == "not_found"
    assert r.headers["content-type"].startswith("application/problem+json")
    assert client.get("/api/files/zip", params={"path": path}).status_code == 404


def test_a_symlink_out_of_the_root_is_refused(client, root):
    try:
        (root / "paper/drafts/leak.md").symlink_to(root / "README.md")
        (root / "paper/drafts/env.md").symlink_to(root / ".env")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not allowed on this machine")
    assert client.get("/api/files/content", params={"path": "paper/drafts/leak.md"}).status_code == 404
    assert client.get("/api/files/content", params={"path": "paper/drafts/env.md"}).status_code == 404
    assert not any("leak.md" in e["path"] or "env.md" in e["path"] for r in client.get("/api/files/tree").json()["roots"] for e in r["entries"])


def test_a_file_or_member_over_the_cap_is_a_413(client, monkeypatch):
    monkeypatch.setattr(files, "MAX_BYTES", 5)
    r = client.get("/api/files/content", params={"path": "paper/drafts/paper.md"})
    assert r.status_code == 413 and r.json()["code"] == "file_too_large"
    z = client.get("/api/files/zip", params={"path": "paper/data/dev/experiment.zip", "member": "experiment.json"})
    assert z.status_code == 413 and z.json()["code"] == "file_too_large"


def test_a_file_type_that_is_not_text_or_an_image_is_a_415(client, root):
    (root / "paper/drafts/x.bin").write_bytes(b"\x00\x01")
    r = client.get("/api/files/content", params={"path": "paper/drafts/x.bin"})
    assert r.status_code == 415 and r.json()["code"] == "file_not_viewable"
