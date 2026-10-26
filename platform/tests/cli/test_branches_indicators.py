"""Branch listing and indicator instances (CLI-DESIGN §3.6)."""

import json


def test_branches_list_shows_the_probe_children(server):
    kids = server("branches", "list", server.run_ids[0], "--json").json
    assert sorted(k["id"] for k in kids) == sorted(server.children)
    assert all(k["branch"]["parent_run_id"] == server.run_ids[0] for k in kids)
    assert server.children[0] in server("branches", "list", server.run_ids[0]).stdout


def test_indicator_series_until_is_a_prefix_of_the_full_series(server):
    rid = server.run_ids[0]
    full = server("indicators", "series", rid, "delta_p", "--json").json
    cut = server("indicators", "series", rid, "delta_p", "--until", "10", "--json").json
    assert cut["t"] == full["t"][:10] and all(cut["outputs"][k] == v[:10] for k, v in full["outputs"].items())
    assert server("indicators", "series", rid, "delta_p", "--until", "3").stdout.splitlines()[0].split()[0] == "t"


def test_indicator_params_errors(server):
    rid = server.run_ids[0]
    bad = server("indicators", "series", rid, "corr_raw", "--params", json.dumps({"window": 99}), "--json")
    assert bad.exit_code == 1 and bad.json["code"] == "indicator_params_invalid" and bad.json["errors"]
    broken = server("indicators", "series", rid, "corr_raw", "--params", "{window", "--json")
    assert broken.exit_code == 1 and broken.json["code"] == "file_invalid"
