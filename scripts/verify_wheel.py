"""Exercise an installed wheel from outside the checkout, using only its CLI.

Run with the clean wheel environment's Python and pass the source checkout solely
to locate static example inputs. No WorldForge source modules are imported here.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path


def verify(source: Path, directory: Path) -> None:
    cli = Path(sys.executable).with_name("worldforge")

    def command(workspace: Path, *arguments: str, expected: int = 0) -> str:
        result = subprocess.run(
            [str(cli), *arguments, "--workspace", str(workspace)],
            cwd=directory,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode != expected:
            raise AssertionError(
                f"{arguments}: expected exit {expected}, got {result.returncode}\n"
                f"{result.stdout}\n{result.stderr}"
            )
        return result.stdout

    payments, mcp, email, composed, oracle = (
        directory / name for name in ("payments", "mcp", "email", "composed", "oracle")
    )
    command(payments, "import", str(source / "examples/payments/openapi.yaml"))
    command(payments, "scenario", "generate", "--count", "2", "--response-lost")
    assert "2/2 passed" in command(payments, "run")
    command(mcp, "import", str(source / "examples/mcp/payments.json"), "--format", "mcp")
    command(mcp, "scenario", "generate", "--count", "2", "--response-lost")
    assert "2/2 passed" in command(mcp, "run", "--agent", "mock-provider")
    provider_report = json.loads(command(mcp, "resources", "aggregate"))
    assert provider_report["metrics"]["provider_requests"]["value"] > 0
    assert provider_report["metrics"]["total_tokens"]["value"] is None
    assert provider_report["metrics"]["estimated_cost_usd"]["value"] is None

    command(email, "import", str(source / "examples/email/tools.json"), "--format", "mcp")
    command(
        composed,
        "world",
        "compose",
        "--service",
        f"payments={payments / 'world.json'}",
        "--service",
        f"email={email / 'world.json'}",
        "--link",
        "email.message.customer_id=payments.customer",
    )
    command(composed, "scenario", "generate", "--namespace", "payments", "--count", "1")
    assert "1/1 passed" in command(composed, "run")

    command(oracle, "import", str(source / "examples/payments/openapi.yaml"))
    discovered = command(
        oracle,
        "falsify",
        "--agent",
        "vulnerable-reference",
        "--search",
        str(source / "examples/oracle/refund-search.json"),
    )
    assert "conditions=2->1" in discovered and "reproduced=3/3" in discovered
    certificates = list((oracle / "failures").glob("WF-F-*.json"))
    assert len(certificates) == 1
    certificate_id = certificates[0].stem
    certificate = json.loads(command(oracle, "failure", "inspect", certificate_id))
    assert certificate["classification"] == "unsafe_retry"
    assert certificate["minimized_conditions"][0]["kind"] == "response_lost"
    assert "Failure reproduced: 3/3" in command(oracle, "failure", "reproduce", certificate_id)
    assert "Failure reproduced: 0/3" in command(
        oracle, "failure", "reproduce", certificate_id, "--agent", "reference", expected=1
    )
    command(oracle, "resources", "aggregate")

    replayed = 0
    for workspace in (payments, mcp, composed, oracle):
        for path in sorted((workspace / "runs").glob("WF-*.json")):
            record = json.loads(path.read_text())
            if workspace == oracle:
                assert len(record["final_state"]["resources"]["refund"]) == 1
            assert "Replay verified" in command(workspace, "replay", path.stem)
            replayed += 1
    print(f"Wheel verified: OpenAPI, MCP/mock provider, composition, and {replayed} replays")
    print(f"Certificate {certificate_id}: minimized 2->1, vulnerable 3/3, safe 0/3")
    print(f"Artifacts: {directory}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--workspace", type=Path)
    options = parser.parse_args()
    root = options.workspace or Path(tempfile.mkdtemp(prefix="worldforge-wheel-v2-"))
    root.mkdir(parents=True, exist_ok=True)
    verify(options.source.resolve(strict=True), root.resolve(strict=True))
