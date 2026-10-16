# Releasing WorldForge

WorldForge v0.1.0 is released. Its tag is immutable: never move, overwrite, delete, or recreate
it. Historical validation is retained in [v0.1-review.md](v0.1-review.md).

The current v0.2 task prepares `worldforge/v0.2-integration-failure-discovery` for independent
review with development package version `0.2.0.dev0`. It creates no v0.2 tag, GitHub release,
or package-registry publication and does not authorize merging. These checks are useful for
reviewing a development candidate; an official future release needs separate maintainer approval.

## Release validation

On the development/review branch with development dependencies installed:

```bash
ruff check .
ruff format --check .
mypy src/worldforge
pytest
python -m build
```

Run the README basic OpenAPI example, custom reference adapter, response-lost scenario, and
replay. For v0.2, also verify static MCP input, composed payments/email worlds, resource reports,
mock provider execution, Failure Oracle minimization, certificate inspect/reproduce, associated
run replay, and safe-agent comparison. Check expected failure exit codes and compatibility/security
limitations. Record actual commands/results; a configured workflow is not remote CI pass evidence.

Verify the wheel from a fresh virtual environment without importing from the source checkout.
The following Linux/macOS example uses temporary paths and an absolute example path:

```bash
REPO_ROOT="$PWD"
VERIFY_ROOT="$(mktemp -d)"
python -m venv "$VERIFY_ROOT/venv"
"$VERIFY_ROOT/venv/bin/python" -m pip install "$REPO_ROOT"/dist/*.whl
cd "$VERIFY_ROOT"
"$VERIFY_ROOT/venv/bin/worldforge" --help
"$VERIFY_ROOT/venv/bin/worldforge" init
"$VERIFY_ROOT/venv/bin/worldforge" import "$REPO_ROOT/examples/payments/openapi.yaml"
"$VERIFY_ROOT/venv/bin/worldforge" scenario generate --count 2 --seed 42
"$VERIFY_ROOT/venv/bin/worldforge" run
"$VERIFY_ROOT/venv/bin/python" - <<'PY'
from pathlib import Path
import subprocess
import sys

cli = Path(sys.executable).with_name("worldforge")
runs = sorted(Path(".worldforge/runs").glob("WF-*.json"))
assert len(runs) == 2, "expected two persisted runs"
for run in runs:
    subprocess.run([str(cli), "replay", run.stem], check=True)
PY
cd "$REPO_ROOT"
```

Run this with exactly one intended wheel in `dist/`; remove stale build artifacts before building
if needed. GitHub CI performs equivalent checks on Python 3.11, 3.12, and 3.13. Keep build outputs
and temporary verification workspaces out of commits.

## Review and publication

Check package metadata and the complete Apache-2.0 license; inspect both wheel and source
distribution. Review `git diff` and `git status` for secrets, private observations, generated
junk, unexpected files, and placeholder features. Confirm documentation matches the installed
CLI. Confirm that package metadata and the changelog agree about development versus release status;
leave an already correct version unchanged. Do not date an unreleased development entry as a release.

Open a review PR containing implementation/architecture changes, CLI commands, schema/version
compatibility, exact validation evidence, manual verification, security notes, limitations, and
explicitly deferred v0.3/v0.4 features. Stop for independent review; do not merge, create another
tag/release, publish a package, or begin the next milestone as part of this task.

For a separately approved future release, maintainers must choose a fresh version/tag and an
explicit approved commit. Existing tags remain immutable. Package-registry publication requires
a separately configured, authorized publishing workflow; this repository's CI does not publish
packages.
