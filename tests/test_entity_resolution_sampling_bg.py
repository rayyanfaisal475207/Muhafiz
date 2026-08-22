"""
Tests for src/ingestion/entity_resolution_sampling_bg.py (Module G3).
"""
import src.ingestion.entity_resolution_sampling_bg as bg
import src.graph.entity_resolution_sampling as sampling


async def test_skips_the_sample_when_the_throttle_roll_misses(monkeypatch):
    monkeypatch.setattr("random.random", lambda: 0.99)
    monkeypatch.setattr(sampling, "SAMPLE_TRIGGER_PROBABILITY", 0.2)
    called = []
    monkeypatch.setattr(sampling, "run_sample", lambda *a, **k: called.append(1))

    await bg._run_entity_resolution_sampling_bg()

    assert called == []


async def test_runs_the_sample_when_the_throttle_roll_hits(monkeypatch):
    monkeypatch.setattr("random.random", lambda: 0.01)
    monkeypatch.setattr(sampling, "SAMPLE_TRIGGER_PROBABILITY", 0.2)

    async def fake_run_sample():
        return {"sampled": 5, "findings": 1}

    monkeypatch.setattr(sampling, "run_sample", fake_run_sample)

    await bg._run_entity_resolution_sampling_bg()  # must not raise


async def test_a_failure_in_run_sample_is_swallowed(monkeypatch):
    monkeypatch.setattr("random.random", lambda: 0.01)
    monkeypatch.setattr(sampling, "SAMPLE_TRIGGER_PROBABILITY", 0.2)

    async def raising_run_sample():
        raise RuntimeError("simulated AGE failure")

    monkeypatch.setattr(sampling, "run_sample", raising_run_sample)

    await bg._run_entity_resolution_sampling_bg()  # must not raise
