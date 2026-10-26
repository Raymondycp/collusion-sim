import sys

import pytest

from simschema.plugins import load_all


@pytest.fixture
def package(tmp_path, monkeypatch):
    root = tmp_path / "fakeplugins"
    root.mkdir()
    (root / "__init__.py").write_text("LOADED = []\n", encoding="utf-8")
    for name in ("b_second", "a_first"):
        (root / f"{name}.py").write_text(f"from fakeplugins import LOADED\nLOADED.append({name!r})\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    yield root
    for mod in [m for m in sys.modules if m == "fakeplugins" or m.startswith("fakeplugins.")]:
        del sys.modules[mod]


def test_load_all_imports_every_module_in_name_order(package):
    import fakeplugins

    assert load_all("fakeplugins") == ["a_first", "b_second"]
    assert fakeplugins.LOADED == ["a_first", "b_second"]


def test_a_plugin_that_fails_to_import_fails_loudly(package):
    (package / "c_broken.py").write_text("raise RuntimeError('broken plug-in')\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="broken plug-in"):
        load_all("fakeplugins")
