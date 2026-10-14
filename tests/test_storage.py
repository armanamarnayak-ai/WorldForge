from __future__ import annotations

import os
from pathlib import Path

import pytest

from worldforge.agents import ReferenceAgent
from worldforge.compiler import compile_openapi
from worldforge.runtime import replay_run, run_scenario_sync
from worldforge.scenarios import generate_scenarios
from worldforge.storage import MAX_RUN_BYTES, JsonRunStore


def make_run():  # type: ignore[no-untyped-def]
    world = compile_openapi(Path(__file__).parents[1] / "examples/payments/openapi.yaml")
    scenario = generate_scenarios(world, count=1, response_lost=True)[0]
    return run_scenario_sync(world, scenario, ReferenceAgent())


def test_roundtrip_preserves_self_contained_run_and_never_overwrites(tmp_path: Path) -> None:
    run = make_run()
    store = JsonRunStore(tmp_path / "runs")
    path = store.save(run)
    before = path.read_bytes()
    assert store.load(run.run_id) == run
    assert replay_run(store.load(run.run_id))
    with pytest.raises(FileExistsError):
        store.save(run)
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".run-*.tmp"))


@pytest.mark.parametrize("identifier", ["../../secret", "/etc/passwd", "WF-ABC", "WF-" + "a" * 33])
def test_invalid_identifiers_cannot_traverse_paths(tmp_path: Path, identifier: str) -> None:
    with pytest.raises(ValueError, match="run_id"):
        JsonRunStore(tmp_path).load(identifier)


def test_symlink_and_fifo_inputs_rejected_without_blocking(tmp_path: Path) -> None:
    identifier = "WF-" + "a" * 32
    source = tmp_path / f"{identifier}.json"
    source.symlink_to(tmp_path / "outside.json")
    with pytest.raises(OSError):
        JsonRunStore(tmp_path).load(identifier)
    source.unlink()
    os.mkfifo(source)
    with pytest.raises(ValueError, match="regular file"):
        JsonRunStore(tmp_path).load(identifier)


def test_oversized_artifact_is_rejected_before_json_parse(tmp_path: Path) -> None:
    identifier = "WF-" + "a" * 32
    source = tmp_path / f"{identifier}.json"
    with source.open("wb") as stream:
        stream.truncate(MAX_RUN_BYTES + 1)
    with pytest.raises(ValueError, match="32 MiB"):
        JsonRunStore(tmp_path).load(identifier)


def test_filename_must_match_embedded_identifier(tmp_path: Path) -> None:
    run = make_run()
    other_id = "WF-" + "a" * 32
    (tmp_path / f"{other_id}.json").write_text(run.model_dump_json(by_alias=True), encoding="utf-8")
    with pytest.raises(ValueError, match="filename"):
        JsonRunStore(tmp_path).load(other_id)


def test_failed_publication_cleans_temporary_artifact(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    run = make_run()

    def fail_link(source, destination):  # type: ignore[no-untyped-def]
        raise OSError("publication failed")

    monkeypatch.setattr(os, "link", fail_link)
    with pytest.raises(OSError, match="publication failed"):
        JsonRunStore(tmp_path).save(run)
    assert list(tmp_path.iterdir()) == []
