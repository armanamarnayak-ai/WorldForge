import json
import re
from pathlib import Path

from typer.testing import CliRunner

from worldforge.cli import app

ROOT = Path(__file__).resolve().parents[1]
runner = CliRunner()


def invoke(arguments, workspace):
    return runner.invoke(app, [*arguments, "--workspace", str(workspace)])


def import_payments(workspace, mcp=False):
    arguments = ["import", str(ROOT / "examples/payments/openapi.yaml")]
    if mcp:
        arguments = ["import", str(ROOT / "examples/mcp/payments.json"), "--format", "mcp"]
    result = invoke(arguments, workspace)
    assert result.exit_code == 0, result.output


def discover(workspace):
    result = invoke(["falsify"], workspace)
    assert result.exit_code == 0, result.output
    assert "conditions=2->1" in result.output
    assert "reproduced=3/3" in result.output
    assert "one_minimal=True" in result.output
    return re.search(r"CERTIFICATE (WF-F-[0-9a-f]{32})", result.output).group(1)


def test_real_oracle_certificate_reproduction_safe_comparison_and_replay(tmp_path):
    import_payments(tmp_path)
    certificate_id = discover(tmp_path)
    inspected = invoke(["failure", "inspect", certificate_id], tmp_path)
    assert inspected.exit_code == 0, inspected.output
    certificate = json.loads(inspected.output)
    assert certificate["classification"] == "unsafe_retry"
    assert certificate["audit"]["total_runs"] == 6
    assert certificate["minimized_conditions"][0]["kind"] == "response_lost"
    assert certificate["agent_configuration"]["adapter"]["selector"] == "vulnerable-reference"
    reproduced = invoke(["failure", "reproduce", certificate_id], tmp_path)
    assert reproduced.exit_code == 0, reproduced.output
    assert "Failure reproduced: 3/3" in reproduced.output
    safe = invoke(["failure", "reproduce", certificate_id, "--agent", "reference"], tmp_path)
    assert safe.exit_code == 1, safe.output
    assert "Failure reproduced: 0/3" in safe.output
    for path in (tmp_path / "runs").glob("*.json"):
        run = json.loads(path.read_text())
        assert len(run["final_state"]["resources"]["refund"]) == 1
        replay = invoke(["replay", path.stem], tmp_path)
        assert replay.exit_code == 0, replay.output


def test_safe_bounded_search_persists_audit_without_certificate(tmp_path):
    import_payments(tmp_path)
    result = invoke(["falsify", "--agent", "reference"], tmp_path)
    assert result.exit_code == 1, result.output
    assert "No verified certificate" in result.output
    assert not (tmp_path / "failures").exists()
    audits = list((tmp_path / "searches").glob("*.json"))
    assert len(audits) == 1
    assert json.loads(audits[0].read_text())["audit"]["candidate_runs"] == 4


def test_mcp_import_reference_mock_provider_and_measured_resources(tmp_path):
    import_payments(tmp_path, mcp=True)
    generated = invoke(["scenario", "generate", "--count", "1", "--response-lost"], tmp_path)
    assert generated.exit_code == 0, generated.output
    result = invoke(["run", "--agent", "mock-provider"], tmp_path)
    assert result.exit_code == 0, result.output
    run_id = re.search(r"RUN (WF-[0-9a-f]{32})", result.output).group(1)
    report = invoke(["resources", "report", run_id], tmp_path)
    assert report.exit_code == 0, report.output
    metrics = json.loads(report.output)["metrics"]
    assert metrics["provider_requests"]["value"] > 0
    assert metrics["input_tokens"]["provenance"] == "UNAVAILABLE"
    assert metrics["estimated_cost_usd"]["value"] is None
    aggregate = invoke(["resources", "aggregate"], tmp_path)
    assert aggregate.exit_code == 0, aggregate.output
    assert json.loads(aggregate.output)["metrics"]["tool_calls"]["value"] == 3


def test_composed_world_cli_generates_and_runs_namespaced_payments(tmp_path):
    payments, email, composed = (tmp_path / name for name in ("payments", "email", "composed"))
    import_payments(payments)
    result = invoke(["import", str(ROOT / "examples/email/tools.json"), "--format", "mcp"], email)
    assert result.exit_code == 0, result.output
    result = invoke(
        [
            "world",
            "compose",
            "--service",
            f"payments={payments / 'world.json'}",
            "--service",
            f"email={email / 'world.json'}",
            "--link",
            "email.message.customer_id=payments.customer",
        ],
        composed,
    )
    assert result.exit_code == 0, result.output
    result = invoke(["scenario", "generate", "--namespace", "payments", "--count", "1"], composed)
    assert result.exit_code == 0, result.output
    result = invoke(["run"], composed)
    assert result.exit_code == 0, result.output
    run_id = re.search(r"RUN (WF-[0-9a-f]{32})", result.output).group(1)
    assert invoke(["replay", run_id], composed).exit_code == 0


def test_certificate_paths_trust_and_aggregation_limits(tmp_path):
    assert invoke(["failure", "inspect", "../../outside"], tmp_path).exit_code == 2
    import_payments(tmp_path)
    rejected = invoke(["falsify", "--agent", "./untrusted.py"], tmp_path)
    assert rejected.exit_code == 2
    assert "--trust-agent-code" in rejected.output
    certificate_id = discover(tmp_path)
    rejected = invoke(["resources", "aggregate", "--limit", "1"], tmp_path)
    assert rejected.exit_code == 2
    assert "exceeding limit" in rejected.output
    # Loading a certificate never grants permission to execute a persisted adapter path.
    path = tmp_path / "failures" / f"{certificate_id}.json"
    certificate = json.loads(path.read_text())
    certificate["agent_configuration"]["adapter"]["selector"] = "./untrusted.py"
    path.write_text(json.dumps(certificate))
    rejected = invoke(["failure", "reproduce", certificate_id], tmp_path)
    assert rejected.exit_code == 2, rejected.output
    assert "not executed automatically" in rejected.output


def test_malformed_search_and_duplicate_services_are_rejected(tmp_path):
    import_payments(tmp_path)
    search = tmp_path / "bad-search.json"
    search.write_text('{"max_runs": 999999999}')
    assert invoke(["falsify", "--search", str(search)], tmp_path).exit_code == 2
    service = f"payments={tmp_path / 'world.json'}"
    assert (
        invoke(["world", "compose", "--service", service, "--service", service], tmp_path).exit_code
        == 2
    )
