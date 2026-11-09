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


def test_retries_with_exponential_backoff_on_429_5xx_529(tiny):
    c = Client([HttpError(429), HttpError(529), HttpError(503), *FINISH])
    op, score, sleeps = run(tiny, c, max_turns=10)
    assert sleeps == [1.0, 2.0, 4.0] and op.usage["retries"] == 3
    assert score["valid"] and score["run_status"] == "completed"


def test_gives_up_after_six_tries_and_marks_invalid(tiny, tmp_path):
    c = Client([HttpError(529)])
    op, score, sleeps = run(tiny, c, "independent_controller", tmp_path)
    assert len(sleeps) == 5 and c.i == 6
    assert score["run_status"] == "operator_error" and score["valid"] is False and not score["safe_state_taken"]


def test_non_retryable_error_not_retried(tiny):
    c = Client([HttpError(400)])
    op, score, sleeps = run(tiny, c)
    assert sleeps == [] and c.i == 1 and score["valid"] is False


def test_token_budget_ends_run_invalid_without_safe_state(tiny, tmp_path):
    c = Client([([tu(1, "read_tags")], "tool_use", Usage(input_tokens=600_000, cache_read_input_tokens=500_000))])
    op, score, _ = run(tiny, c, "independent_controller", tmp_path, max_input_tokens=1_000_000)
    assert c.i == 2 or c.i == 1
    assert score["run_status"] == "token_budget_exceeded" and score["valid"] is False and not score["safe_state_taken"]
    meta = json.loads(next(tmp_path.iterdir()).joinpath("meta.json").read_text())
    assert "token_budget_exceeded" in meta["invalid_reason"] and meta["usage"]["input_tokens"] >= 600_000


def test_default_budget_is_two_million():
    assert ApiOperator().max_input_tokens == 2_000_000


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
