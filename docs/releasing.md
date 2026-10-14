# Releasing WorldForge

v0.1 is a release candidate until maintainers explicitly approve it. Do not start v0.2, merge the
candidate, publish a package, or create a release tag automatically as part of candidate review.

## Candidate validation

On the candidate branch with development dependencies installed:

```bash
ruff check .
ruff format --check .
mypy src/worldforge
pytest
python -m build
```

Run the README example, custom reference adapter, response-lost scenario, and replay. Check
failure exit codes and review compatibility/security limitations. Record the actual commands and
results; a configured workflow is not evidence that remote CI has passed.

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
CLI and update the changelog/version together when approving a release.

Open a PR containing implemented behavior, architecture decisions, validation evidence, manual
verification, known limitations, and the proposed next milestone. Do not merge it automatically.
After maintainer approval, maintainers can tag the approved commit and create a GitHub release
with the matching changelog and artifacts. Package-registry publication requires a separately
configured, authorized publishing workflow; this repository's CI does not publish packages.
