"""Keep simulated timings separate from measured filesystem traversal."""

import importlib.util
import json
from pathlib import Path

import pytest


def module():
    path = Path(__file__).resolve().parents[1] / "scripts/benchmark_growth.py"
    spec = importlib.util.spec_from_file_location("benchmark_growth", path)
    benchmark = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(benchmark)
    return benchmark


async def test_growth_benchmark_uses_fresh_caches_and_records_every_checkpoint(tmp_path):
    benchmark = module()
    output = tmp_path / "report.json"
    report = await benchmark.benchmark([128, 257], output)
    assert report["complete"]
    assert [row["entries"] for row in report["samples"]] == [128, 257]
    assert report == json.loads(output.read_text())
    assert "synthetic" in report["kind"]
    assert report["samples"][-1]["snapshot_seconds"] > 0


@pytest.mark.parametrize("time2,expected", [(20, 40), (40, 160)])
def test_forecast_distinguishes_linear_and_quadratic_growth(time2, expected):
    model = module().forecast(
        [{"entries": 100, "seconds": 10}, {"entries": 200, "seconds": time2}], 400, 3
    )
    assert model["fitted_scenario_seconds"] == expected + 3
    assert "not a measured" in model["warning"]
