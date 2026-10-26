"""Output modes, error shapes and exit codes (CLI-DESIGN §4)."""

import httpx2


def fake(handler):
    return httpx2.Client(base_url="http://fake/api", transport=httpx2.MockTransport(handler))


def test_json_before_and_after_the_subcommand_is_the_api_body(server):
    body = server.http.get("health").json()
    assert server("--json", "health").json == body
    assert server("health", "--json").json == body


def test_human_output_is_plain_text(server):
    out = server("runs", "list").stdout
    header = out.splitlines()[0].split()
    assert header[:3] == ["id", "kind", "status"] and server.run_ids[0] in out
    assert "\x1b[" not in out and not out.lstrip().startswith("{")


def test_a_problem_goes_to_stdout_as_json_and_to_stderr_as_one_line(server):
    r = server("runs", "show", "run_nope", "--json")
    assert r.exit_code == 1
    assert r.json["code"] == "not_found" and r.json["status"] == 404
    assert r.stderr.startswith("error not_found (HTTP 404): ")
    human = server("runs", "show", "run_nope")
    assert human.exit_code == 1 and human.stdout == "" and "not_found" in human.stderr


def test_a_missing_argument_is_a_usage_error_with_exit_2(server):
    r = server("--json", "runs", "show")
    message = "Missing argument 'RUN_ID'."
    assert r.exit_code == 2 and r.json == {"code": "cli_usage", "status": None, "message": message, "error": message, "type": "cli_usage"}


def test_a_refused_connection_is_server_unreachable_with_exit_3(run_cli):
    def refuse(request):
        raise httpx2.ConnectError("refused", request=request)

    r = run_cli(fake(refuse), "--json", "health")
    assert r.exit_code == 3 and r.json["code"] == "server_unreachable" and r.json["status"] is None


def test_a_5xx_without_a_problem_body_is_server_unreachable(run_cli):
    r = run_cli(fake(lambda request: httpx2.Response(502, text="Bad Gateway")), "--json", "health")
    assert r.exit_code == 3 and r.json["code"] == "server_unreachable" and "502" in r.json["message"]


def test_a_route_that_is_not_built_passes_its_problem_through(run_cli):
    problem = {"title": "not built", "status": 501, "code": "not_built", "message": "Unit 7b."}
    r = run_cli(fake(lambda request: httpx2.Response(501, json=problem)), "--json", "health")
    assert r.exit_code == 1 and r.json == {**problem, "error": "Unit 7b.", "type": "not_built"}


def test_hub_keys_copy_message_and_code_but_keep_the_problem_type_uri(server, run_cli):
    api_problem = server("runs", "show", "run_nope", "--json").json
    sent = server.http.get("runs/run_nope").json()
    assert {k: api_problem[k] for k in sent} == sent and api_problem["error"] == sent["message"]
    assert api_problem["type"] == sent["type"] and sent["type"].endswith("/not_found")

    def refuse(request):
        raise httpx2.ConnectError("refused", request=request)

    cli_error = run_cli(fake(refuse), "--json", "health").json
    assert cli_error["type"] == cli_error["code"] == "server_unreachable" and cli_error["error"] == cli_error["message"]


def test_ctrl_c_is_cli_aborted_with_exit_1(run_cli):
    def interrupt(request):
        raise KeyboardInterrupt

    r = run_cli(fake(interrupt), "--json", "health")
    assert r.exit_code == 1 and r.json["code"] == "cli_aborted"


def test_an_id_cannot_change_the_route(server, run_cli):
    paths = []

    def record(request):
        paths.append(request.url.raw_path.decode())
        return httpx2.Response(404, json={"title": "not found", "status": 404, "code": "not_found", "message": "no"})

    for bad in ("x/../../ledger", "a?status=failed", "a#b"):
        assert server("runs", "show", bad, "--json").json["code"] == "not_found"
        run_cli(fake(record), "runs", "show", bad)
    assert paths == ["/api/runs/x%2F..%2F..%2Fledger", "/api/runs/a%3Fstatus%3Dfailed", "/api/runs/a%23b"]
    for dots in (".", ".."):
        for args in (["runs", "show", dots], ["runs", "stop", dots], ["scenarios", "delete", dots]):
            r = run_cli(fake(record), *args, "--json")
            assert r.exit_code == 2 and r.json["code"] == "cli_usage"
    assert len(paths) == 3
