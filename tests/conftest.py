"""Shared fixtures."""

from pathlib import Path

import pytest

from testbed.scenario import Scenario, load_scenario
from testbed.sim import Simulation
from testbed.tools import ToolBox

FIXTURE = Path(__file__).parent / "fixtures" / "tiny_scenario.yaml"


@pytest.fixture
def tiny() -> Scenario:
    return load_scenario(FIXTURE)


def make_scenario(**kw) -> Scenario:
    """Quiet scenario (no scheduled events) for unit tests."""
    base = dict(id="t", title="t", description="", briefing="b", duration_min=120)
    base.update(kw)
    return Scenario.from_dict(base)


@pytest.fixture
def make_sim():
    def _make(condition="human_equivalent", seed=1, **scenario_kw):
        sim = Simulation(make_scenario(**scenario_kw), condition, seed)
        return sim, ToolBox(sim)
    return _make
