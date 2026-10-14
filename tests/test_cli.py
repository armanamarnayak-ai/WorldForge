import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from worldforge.cli import app, read_text

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "examples/payments/openapi.yaml"


def invoke(arguments, workspace):
    return runner.invoke(app, [*arguments, "--workspace", str(workspace)])


@pytest.mark.parametrize("lost", [False, True])
def test_full_cli_workflow_and_replay(tmp_path, lost):
    workspace = tmp_path / "artifacts"
    assert invoke(["init"], workspace).exit_code == 0
    imported = invoke(["import", str(SPEC)], workspace)
    assert imported.exit_code == 0, imported.output
    assert "UNSUPPORTED 0" in imported.output
    inspected = invoke(["world", "inspect"], workspace)
    assert inspected.exit_code == 0
    assert json.loads(inspected.output)["world_id"] == "payments"
    args = ["scenario", "generate", "--count", "2", "--seed", "123"]
    if lost:
        args += ["--response-lost"]
    generated = invoke(args, workspace)
    assert generated.exit_code == 0, generated.output
    executed = invoke(["run"], workspace)
    assert executed.exit_code == 0, executed.output
    assert "2/2 passed" in executed.output
    ids = re.findall(r"RUN (WF-[0-9a-f]{32}) PASS", executed.output)
    assert len(ids) == 2
    for run_id in ids:
        record = json.loads((workspace / "runs" / f"{run_id}.json").read_text())
        assert record["evaluation"]["usage"]["retries"] == int(lost)
        replayed = invoke(["replay", run_id], workspace)
        assert replayed.exit_code == 0, replayed.output
        assert "Replay verified" in replayed.output


def test_local_files_not_overwritten_without_force(tmp_path):
    assert invoke(["import", str(SPEC)], tmp_path).exit_code == 0
    path = tmp_path / "world.json"
    before = path.read_bytes()
    failed = invoke(["import", str(SPEC)], tmp_path)
    assert failed.exit_code == 2
    assert "already exists" in failed.output
    assert path.read_bytes() == before
    assert invoke(["import", str(SPEC), "--force"], tmp_path).exit_code == 0


def test_useful_missing_file_and_budget_errors(tmp_path):
    result = invoke(["run"], tmp_path)
    assert result.exit_code == 2
    assert "Import a world first" in result.output
    result = invoke(["scenario", "generate", "--count", "0"], tmp_path)
    assert result.exit_code == 2
    result = invoke(["replay", "../../outside"], tmp_path)
    assert result.exit_code == 2


def test_empty_scenario_batch_is_not_a_pass(tmp_path):
    assert invoke(["import", str(SPEC)], tmp_path).exit_code == 0
    (tmp_path / "scenarios.json").write_text("[]")
    result = invoke(["run"], tmp_path)
    assert result.exit_code == 2
    assert "no matching scenarios" in result.output


def test_cli_rejects_devices_and_bounds_reads(tmp_path):
    with pytest.raises(ValueError, match="regular file"):
        read_text(Path("/dev/zero"))
    oversized = tmp_path / "large.json"
    with oversized.open("wb") as stream:
        stream.truncate(8 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="8 MiB"):
        read_text(oversized)


def test_python_adapter_requires_explicit_trust_and_works(tmp_path):
    assert invoke(["import", str(SPEC)], tmp_path).exit_code == 0
    assert invoke(["scenario", "generate", "--count", "1"], tmp_path).exit_code == 0
    adapter = tmp_path / "adapter.py"
    adapter.write_text(
        "from worldforge.agents import ReferenceAgent\n"
        "def create_agent():\n    return ReferenceAgent()\n"
    )
    result = invoke(["run", "--agent", str(adapter)], tmp_path)
    assert result.exit_code == 2
    assert "--trust-agent-code" in result.output
    result = invoke(["run", "--agent", str(adapter), "--trust-agent-code"], tmp_path)
    assert result.exit_code == 0, result.output
    assert "1/1 passed" in result.output


def test_failed_agent_run_is_persisted_and_returns_failure(tmp_path):
    assert invoke(["import", str(SPEC)], tmp_path).exit_code == 0
    assert invoke(["scenario", "generate", "--count", "1"], tmp_path).exit_code == 0
    adapter = tmp_path / "quitter.py"
    adapter.write_text(
        "from worldforge.models import AgentDecision\n"
        "class Agent:\n"
        "    metadata = {'name': 'quitter'}\n"
        "    async def reset(self): pass\n"
        "    async def act(self, observation): return AgentDecision(message='finished')\n"
        "    async def shutdown(self): pass\n"
        "def create_agent(): return Agent()\n"
    )
    result = invoke(["run", "--agent", str(adapter), "--trust-agent-code"], tmp_path)
    assert result.exit_code == 1, result.output
    assert "0/1 passed" in result.output
    records = list((tmp_path / "runs").glob("*.json"))
    assert len(records) == 1
    assert not json.loads(records[0].read_text())["evaluation"]["success"]
