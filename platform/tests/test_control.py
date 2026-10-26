from simcore.agents import PolicyContext
from simcore.recorder import MemorySink, Recorder
from simcore.run import Run
from simcore.scripted import make_scripted
from simkit import JOINT, NASH, capability, frozen_scenario, of, spec


class Script:
    """check(t) answers from a dict; wait() answers the next queued reply."""

    def __init__(self, at, after_wait=()):
        self.at, self.after_wait, self.waits = at, list(after_wait), 0

    def check(self, t):
        return self.at.get(t, "go")

    def wait(self):
        self.waits += 1
        return self.after_wait.pop(0)


def run_with(control):
    scenario = frozen_scenario(rounds=8)
    sp = spec({s: capability() for s in "AB"}, {"A": "constant", "B": "constant"})
    market = scenario.market.build()
    pols = {
        s: make_scripted("constant", PolicyContext(seller_id=s, sellers=("A", "B"), market=market, params=scenario.params), price=p)
        for s, p in (("A", NASH), ("B", JOINT))
    }
    sink = MemorySink()
    rec = Recorder(sp.run_id, [sink])
    Run(sp, scenario, pols, rec, control=control).execute()
    return sink


def test_stop_ends_the_run_at_a_round_boundary():
    sink = run_with(Script({5: "stop"}))
    end = of(sink, "run_ended")[0]
    assert (end.status, end.end_reason, end.t) == ("stopped", "stopped_by_user", 4)
    assert max(e.t for e in of(sink, "settled")) == 4


def test_pause_then_resume_records_both_and_finishes():
    control = Script({3: "pause"}, after_wait=["go"])
    sink = run_with(control)
    statuses = [(e.t, e.status) for e in of(sink, "run_status_changed")]
    assert (2, "paused") in statuses and (2, "running") in statuses and control.waits == 1
    assert of(sink, "run_ended")[0].status == "completed"


def test_stop_while_paused():
    sink = run_with(Script({3: "pause"}, after_wait=["stop"]))
    assert of(sink, "run_ended")[0].end_reason == "stopped_by_user"
