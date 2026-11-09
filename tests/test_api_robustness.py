"""ApiOperator: retries, token budget, prompt caching, usage logging, stop-reason handling, validity."""

import json
from types import SimpleNamespace

import pytest

from testbed.operators import ApiOperator, with_cache_breakpoint
from testbed.runner import run_scenario


class Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def model_dump(self, **_):
        return dict(self.__dict__)


class Usage:
    def __init__(self, **kw):
        self.d = {"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, **kw}
        self.__dict__.update(self.d)

    def model_dump(self):
        return dict(self.d)


class HttpError(Exception):
    def __init__(self, status):
        super().__init__(f"http {status}")
        self.status_code = status


def tu(i, name, **inp):
    return Block(type="tool_use", id=f"tu{i}", name=name, input=inp)


class Client:
    """Plays a script of (blocks, stop_reason[, usage]) or Exception entries; records every request."""

    def __init__(self, script):
        self.script, self.i, self.seen = script, 0, []
        self.messages = self

    def create(self, **kw):
        self.seen.append(json.loads(json.dumps(kw, default=lambda o: o.__dict__)))
        item = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        if isinstance(item, Exception):
            raise item
        blocks, stop, *rest = item
        return SimpleNamespace(content=blocks, stop_reason=stop, model=kw["model"], usage=rest[0] if rest else Usage())


FINISH = [([tu(1, "wait", minutes=60)], "tool_use"), ([tu(2, "wait", minutes=60)], "tool_use"),
          ([tu(3, "write_shift_log", text="final log")], "tool_use")]


def run(tiny, client, cond="human_equivalent", out=None, **kw):
    sleeps = []
    op = ApiOperator(client=client, sleep=sleeps.append, **kw)
    _, score = run_scenario(tiny, op, cond, 1, out)
    return op, score, sleeps


def test_retries_with_capped_exponential_backoff_on_429_5xx_529(tiny):
    c = Client([HttpError(429), HttpError(529), HttpError(503), *FINISH])
    op, score, sleeps = run(tiny, c, max_turns=10)
    assert len(sleeps) == 3 and op.usage["retries"] == 3
    for got, nominal in zip(sleeps, (2.0, 4.0, 8.0)):          # +-20% jitter around 2, 4, 8
        assert 0.8 * nominal <= got <= 1.2 * nominal
    assert score["valid"] and score["run_status"] == "completed"


def test_backoff_schedule_is_about_two_minutes_and_capped():
    from testbed.apiutil import backoff_delays
    d = backoff_delays(rng=lambda: 0.5)                          # jitter-free midpoint
    assert d == [2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0] and 115 <= sum(d) <= 125
    assert all(0.8 * n <= g <= 1.2 * n for g, n in zip(backoff_delays(rng=lambda: 0.99), d))


def test_gives_up_after_eight_tries_and_marks_invalid(tiny, tmp_path):
    c = Client([HttpError(529)])
    op, score, sleeps = run(tiny, c, "independent_controller", tmp_path)
    assert len(sleeps) == 7 and c.i == 8 and 90 <= sum(sleeps) <= 150
    assert score["run_status"] == "operator_error" and score["valid"] is False and not score["safe_state_taken"]


def test_non_retryable_error_not_retried(tiny):
    c = Client([HttpError(400)])
    op, score, sleeps = run(tiny, c)
    assert sleeps == [] and c.i == 1 and score["valid"] is False


def test_token_budget_is_cost_weighted_and_ends_run_invalid_without_safe_state(tiny, tmp_path):
    # weighted = input + 1.25*cache_write + 0.1*cache_read + 5*output
    u = Usage(input_tokens=100_000, output_tokens=100_000, cache_read_input_tokens=500_000, cache_creation_input_tokens=80_000)
    c = Client([([tu(1, "read_tags")], "tool_use", u)])
    op, score, _ = run(tiny, c, "independent_controller", tmp_path, max_weighted_tokens=700_000)
    assert op.weighted_tokens_used == 100_000 + 1.25 * 80_000 + 0.1 * 500_000 + 5 * 100_000 == 750_000
    assert score["run_status"] == "token_budget_exceeded" and score["valid"] is False and not score["safe_state_taken"]
    meta = json.loads(next(tmp_path.iterdir()).joinpath("meta.json").read_text())
    assert "token_budget_exceeded" in meta["invalid_reason"] and meta["usage"]["input_tokens"] == 100_000


def test_big_cache_reads_alone_do_not_trip_the_weighted_budget(tiny):
    u = Usage(input_tokens=1_000, output_tokens=100, cache_read_input_tokens=300_000)   # old raw-input rule would count 301k
    c = Client([(b, s, u) for b, s in FINISH])
    op, score, _ = run(tiny, c, max_weighted_tokens=400_000)
    assert score["valid"] and op.weighted_tokens_used == 3 * (1_000 + 30_000 + 500)


def test_default_budget_and_turns():
    from testbed.operators import DEFAULT_MAX_WEIGHTED_TOKENS
    op = ApiOperator()
    assert op.max_weighted_tokens == DEFAULT_MAX_WEIGHTED_TOKENS == 6_000_000 and op.max_turns == 250 and op.max_usd is None
    from testbed.scenario import Scenario
    assert Scenario.__dataclass_fields__["max_turns"].default == 250


def test_usd_cap_ends_run_invalid_and_estimated_usd_in_meta(tiny, tmp_path):
    u = Usage(input_tokens=4_000_000, output_tokens=400_000)       # haiku: 0.4 + 0.2 = $0.60
    c = Client([([tu(1, "read_tags")], "tool_use", u)])
    op, score, _ = run(tiny, c, out=tmp_path, model="claude-haiku-5-5", max_usd=0.5)
    assert score["run_status"] == "usd_budget_exceeded" and score["valid"] is False
    meta = json.loads(next(tmp_path.iterdir()).joinpath("meta.json").read_text())
    assert meta["estimated_usd"] == pytest.approx(0.6) and meta["cost_breakdown"]["operator"]["usd"] == pytest.approx(0.6)


def test_price_table():
    from testbed.apiutil import estimated_usd, price_for
    u = {"input_tokens": 1_000_000, "output_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000,
         "cache_creation_input_tokens": 1_000_000}
    # Real 5.5 list prices: in, out, cache read (Haiku 0.1x, Sonnet/Opus 0.05x), cache write 1.25x.
    assert estimated_usd(u, "claude-haiku-5-5") == pytest.approx(0.10 + 0.50 + 0.01 + 0.125)
    assert estimated_usd(u, "claude-sonnet-5-5") == pytest.approx(2 + 10 + 0.10 + 2.5)
    assert estimated_usd(u, "claude-opus-5-5") == pytest.approx(4 + 20 + 0.20 + 5)
    assert price_for("mystery-model")[1] is True            # unknown model priced as opus, flagged


def test_prompt_caching_breakpoints_move_forward(tiny, tmp_path):
    c = Client(FINISH)
    run(tiny, c, out=tmp_path, max_turns=10)
    first, last = c.seen[0], c.seen[-1]
    assert first["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert all("cache_control" not in t for t in first["tools"][:-1])
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    for req in c.seen:
        markers = [(i, b) for i, m in enumerate(req["messages"]) if isinstance(m["content"], list)
                   for b in m["content"] if isinstance(b, dict) and "cache_control" in b]
        assert len(markers) == 1 and markers[0][0] == len(req["messages"]) - 1     # only the newest message
    assert len(last["messages"]) > len(first["messages"])
    transcript = (next(tmp_path.iterdir()) / "transcript.jsonl").read_text()
    assert "cache_control" not in transcript                                        # raw transcript untouched


def test_with_cache_breakpoint_does_not_mutate_input():
    msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": [{"type": "text", "text": "yo"}]}]
    out = with_cache_breakpoint(msgs)
    assert "cache_control" not in json.dumps(msgs) and out[-1]["content"][-1]["cache_control"]


def test_usage_accumulates_into_meta(tiny, tmp_path):
    c = Client([(b, s, Usage(input_tokens=1000, output_tokens=50, cache_read_input_tokens=300, cache_creation_input_tokens=200))
                for b, s in FINISH])
    op, score, _ = run(tiny, c, out=tmp_path)
    meta = json.loads(next(tmp_path.iterdir()).joinpath("meta.json").read_text())
    assert meta["usage"] == {"input_tokens": 3000, "output_tokens": 150, "cache_read_input_tokens": 900,
                             "cache_creation_input_tokens": 600, "requests": 3, "retries": 0}


def test_tool_results_are_compact_json(tiny):
    c = Client([([tu(1, "read_tags")], "tool_use"), *FINISH])
    run(tiny, c)
    content = c.seen[1]["messages"][-1]["content"][0]["content"]
    assert ": " not in content and ", " not in content and "\n" not in content
    assert json.loads(content)["ok"] is True


def test_max_tokens_with_tool_use_does_not_execute(tiny):
    c = Client([([tu(1, "stop_unit", unit="U1")], "max_tokens"), *FINISH])
    op, score, _ = run(tiny, c)
    res = c.seen[1]["messages"][-1]["content"][0]
    assert res["is_error"] and "NOT executed" in res["content"]
    assert score["valid"]


def test_max_tokens_text_only_gets_nudge(tiny):
    c = Client([([Block(type="text", text="blah")], "max_tokens"), *FINISH])
    run(tiny, c)
    assert "cut off" in json.dumps(c.seen[1]["messages"][-1])


def test_refusal_ends_run_invalid(tiny):
    c = Client([([Block(type="text", text="I can't")], "refusal")])
    op, score, _ = run(tiny, c)
    assert score["run_status"] == "model_refusal" and score["valid"] is False and c.i == 1


def test_pause_turn_resends_without_new_user_message(tiny):
    c = Client([([Block(type="text", text="working")], "pause_turn"), *FINISH])
    run(tiny, c)
    assert c.seen[1]["messages"][-1]["role"] == "assistant"
    assert len(c.seen[1]["messages"]) == len(c.seen[0]["messages"]) + 1


def test_no_safe_state_from_infrastructure_failure_but_link_drop_does(tiny, tmp_path):
    c = Client([HttpError(500)])
    _, bad, _ = run(tiny, c, "independent_controller", tmp_path)
    assert not bad["safe_state_taken"]
    from testbed.operators import MockOperator
    _, ok = run_scenario(tiny, MockOperator("cautious"), "independent_controller", 1, tmp_path, link_drop_at=15)
    assert ok["safe_state_taken"] and ok["valid"]
