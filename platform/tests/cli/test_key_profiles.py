"""Provider profiles and keys through the CLI: a key comes from stdin only and is never printed or stored (CLI-DESIGN §3.4, §5)."""

import httpx2
import pytest

from simcli.client import Api, DryRun

KEY = "sk-cli-test-0123456789abcdef"
NEW_KEY = "sk-cli-new-9876543210zyxwvu"
SECRETS = (KEY, NEW_KEY)


def no_key_in(*texts: str) -> bool:
    return not any(secret in text for text in texts for secret in SECRETS)


def no_key_on_disk(root) -> bool:
    files = [f for f in root.rglob("*") if f.is_file()]
    return files != [] and not any(secret.encode() in f.read_bytes() for f in files for secret in SECRETS)


def add(server, *extra: str, key: str = KEY):
    args = ["profiles", "add", "--name", "Local test", "--base-url", "https://llm.example.test/v1", "--default-model", "m-small"]
    return server(*args, *extra, "--json", input=key + "\n")


@pytest.fixture
def profile(server):
    r = add(server)
    assert r.exit_code == 0, r.stderr
    yield r.json["id"]
    server.http.delete(f"provider-profiles/{r.json['id']}")


def test_add_reads_the_key_from_stdin_and_prints_only_the_hint(server, profile):
    shown = server("profiles", "show", profile, "--json")
    assert shown.json["key_set"] and shown.json["key_hint"] == "sk-…cdef" and "api_key" not in shown.json
    assert server.ring.data[("collusion-sim", profile)] == KEY
    listing = server("profiles", "list")
    assert profile in listing.stdout and "sk-…cdef" in listing.stdout
    assert no_key_in(shown.stdout, shown.stderr, listing.stdout, server("profiles", "list", "--json").stdout)
    assert no_key_on_disk(server.root)


def test_set_key_replaces_the_key(server, profile):
    r = server("profiles", "set-key", profile, "--json", input=NEW_KEY + "\n")
    assert r.exit_code == 0 and r.json["key_hint"] == "sk-…" + NEW_KEY[-4:]
    assert server.ring.data[("collusion-sim", profile)] == NEW_KEY
    assert no_key_in(r.stdout, r.stderr) and no_key_on_disk(server.root)


def test_an_empty_key_is_refused_before_any_request(server):
    before = server.http.get("provider-profiles").json()
    for stdin in ("", "\n", "   \n"):
        r = add(server, key=stdin.rstrip("\n"))
        assert r.exit_code == 2 and r.json["code"] == "cli_usage"
    assert server.http.get("provider-profiles").json() == before


def test_a_key_typed_as_an_argument_is_refused_and_not_echoed(server, profile):
    for args in (
        ["profiles", "set-key", profile, KEY],
        ["profiles", "add", "--name", "x", "--base-url", "https://x.test/v1", f"--api-key={KEY}"],
        ["profiles", "add", "--name", "x", "--base-url", "https://x.test/v1", "--api-key", KEY],
        ["runs", "list", "--limit", KEY],
    ):
        r = server(*args, "--json", input="")
        assert r.exit_code == 2 and no_key_in(r.stdout, r.stderr), args
    assert server.ring.data[("collusion-sim", profile)] == KEY


def test_a_server_validation_error_does_not_echo_the_key(server):
    r = server("profiles", "add", "--name", "", "--base-url", "ftp://x", "--json", input=KEY + "\n")
    assert r.exit_code == 1 and r.json["code"] == "validation_error" and no_key_in(r.stdout, r.stderr)


def test_update_changes_fields_without_the_key(server, profile):
    r = server("profiles", "update", profile, "--default-model", "m-large", "--input-price", "0.1", "--output-price", "0.4", "--json")
    assert r.exit_code == 0 and r.json["default_model"] == "m-large" and r.json["pricing"]["output_usd_per_mtok"] == 0.4
    assert server.ring.data[("collusion-sim", profile)] == KEY
    assert server("profiles", "update", profile, "--json").exit_code == 2
    assert server("profiles", "update", profile, "--input-price", "0.1", "--json").exit_code == 2
    dry = server("profiles", "update", profile, "--name", "renamed", "--dry-run", "--json").json
    assert dry["method"] == "PATCH" and dry["body"] == {"name": "renamed"}
    assert server.http.get(f"provider-profiles/{profile}").json()["name"] == "Local test"


def test_key_commands_have_no_dry_run_and_a_dry_run_never_shows_a_key():
    sent = []
    api = Api(httpx2.Client(base_url="http://fake/api", transport=httpx2.MockTransport(sent.append)), dry_run=True)
    with pytest.raises(DryRun) as d:
        api.post("provider-profiles", {"name": "x", "api_key": KEY})
    assert d.value.payload["body"] == {"name": "x", "api_key": "(not shown)"} and sent == []


def test_delete_removes_the_profile_and_its_key(server):
    pid = add(server).json["id"]
    assert server("profiles", "delete", pid, "--json").json == {"deleted": pid}
    assert ("collusion-sim", pid) not in server.ring.data
    assert server("profiles", "show", pid, "--json").json["code"] == "not_found"


def test_profile_test_needs_allow_paid_and_the_server_switch_still_refuses(server, profile, run_cli, monkeypatch):
    monkeypatch.delenv("SIM_ALLOW_PAID", raising=False)
    posts = []
    http = httpx2.Client(base_url="http://fake/api", transport=httpx2.MockTransport(lambda req: posts.append(req) or httpx2.Response(500)))
    r = run_cli(http, "profiles", "test", "prof_x", "--json")
    assert r.exit_code == 1 and r.json["code"] == "paid_flag_required" and posts == []
    served = server("profiles", "test", profile, "--allow-paid", "--json")
    assert served.exit_code == 1 and served.json["code"] == "paid_calls_disabled"


def test_models_passes_the_server_answer_through(run_cli):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx2.Response(200, json={"models": ["m-a", "m-b"], "available": True})

    http = httpx2.Client(base_url="http://fake/api", transport=httpx2.MockTransport(handler))
    r = run_cli(http, "profiles", "models", "prof_x", "--json")
    assert r.json == {"models": ["m-a", "m-b"], "available": True} and paths == ["/api/provider-profiles/prof_x/models"]


def test_a_key_command_usage_error_hides_every_value_whatever_its_shape(server, profile):
    for secret in ("abc", "letters-and-dashes-only", "x1"):
        for args in (
            ["profiles", "set-key", profile, secret],
            ["profiles", "add", "--name", "x", "--base-url", "https://x.test/v1", f"--api-key={secret}"],
            ["profiles", "add", "--name", "x", "--base-url", "https://x.test/v1", "extra", secret],
        ):
            r = server(*args, "--json", input="")
            assert r.exit_code == 2 and secret not in r.stdout and secret not in r.stderr, args
    other = server("runs", "list", "--limit", "abc", "--json")
    assert other.exit_code == 2 and "abc" in other.json["message"]


def test_stdin_that_is_not_a_tty_gets_a_warning_before_the_read(server, profile):
    from simcli.profiles import NOT_A_TTY

    r = server("profiles", "set-key", profile, "--json", input=KEY + "\n")
    assert r.exit_code == 0 and NOT_A_TTY in r.stderr and no_key_in(r.stdout, r.stderr)
