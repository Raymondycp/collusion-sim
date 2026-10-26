"""runs and experiments archive / unarchive (DECISIONS-29): ids or --all, --include-archived on list, nothing deleted."""


def listed(server, *extra: str) -> set[str]:
    return {r["id"] for r in server("runs", "list", "--json", *extra).json["items"]}


def test_runs_archive_ids_and_all_hide_and_restore_and_keep_show(server):
    r0, r1 = server.run_ids
    before = listed(server)
    assert server("runs", "archive", r0, "--json").json == {"count": 1, "skipped": []}
    assert r0 not in listed(server) and r0 in listed(server, "--include-archived")
    assert server("runs", "show", r0, "--json").json["archived_at"], "a read by id"
    done = server("runs", "archive", "--all", "--json").json
    assert done["count"] == len(before) - 1 and done["skipped"] == [] and listed(server) == set()
    assert server("runs", "unarchive", "--all", "--json").json["count"] == len(before)
    assert listed(server) == before and r1 in before


def test_ids_or_all_not_both_and_dry_run_sends_nothing(server):
    r0 = server.run_ids[0]
    for args, message in (((), "Give ids or --all."), ((r0, "--all"), "Give ids or --all, not both.")):
        out = server("runs", "archive", *args, "--json")
        assert out.exit_code == 2 and out.json["code"] == "cli_usage" and out.json["message"] == message
    preview = server("runs", "archive", "--all", "--dry-run", "--json").json
    assert preview["dry_run"] and preview["path"] == "/runs/archive" and preview["body"] == {"all": True}
    assert r0 in listed(server)


def test_experiments_archive_reports_an_unknown_id_and_list_takes_include_archived(server):
    out = server("experiments", "archive", "exp_unknown", "--json")
    assert out.exit_code == 1 and out.json["code"] == "not_found"
    assert server("experiments", "list", "--include-archived", "--json").exit_code == 0
    assert server("experiments", "unarchive", "--all", "--json").json == {"count": 0, "skipped": []}
