# Releasing WorldForge

WorldForge v0.1.0 is released. Its tag is immutable: never move, overwrite, delete, or recreate
it. Historical validation is retained in [v0.1-review.md](v0.1-review.md).

The v0.2 implementation was reviewed in PR #3, passed CI on Python 3.11, 3.12, and 3.13, and
was squash-merged into `main`. Prepare the official v0.2.0 package metadata and documentation on
`worldforge/v0.2.0-release`, starting from the latest approved `main`. Release preparation does
not create a tag, GitHub Release, or package-registry publication.

## Release validation

On the release-preparation branch with development dependencies installed:

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
"$VERIFY_ROOT/venv/bin/python" -m pip install "$REPO_ROOT/dist/worldforge_engine-0.2.0-py3-none-any.whl"
cd "$VERIFY_ROOT"
"$VERIFY_ROOT/venv/bin/worldforge" --help
"$VERIFY_ROOT/venv/bin/worldforge" version  # Must report 0.2.0.
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
"$VERIFY_ROOT/venv/bin/python" "$REPO_ROOT/scripts/verify_wheel.py" --source "$REPO_ROOT"
cd "$REPO_ROOT"
```

The explicit wheel filename avoids installing an older build from `dist/`. The verification script
also exercises MCP/mock provider, composition, resources, Oracle/certificates, and safe comparison.
GitHub CI performs equivalent checks on Python 3.11, 3.12, and 3.13. Keep build outputs
and temporary verification workspaces out of commits.

## Review and publication

Check package metadata and the complete Apache-2.0 license; inspect both wheel and source
distribution. Review `git diff` and `git status` for secrets, private observations, generated
junk, unexpected files, and placeholder features. Confirm documentation matches the installed
CLI. Confirm that package metadata consistently reports `0.2.0` and that the changelog release
entry is dated `2026-10-17`. Preserve the v0.1.0 entry and historical validation evidence.

Open a release-preparation PR into `main` describing the version/documentation changes, exact
validation evidence, manual verification, and unchanged functionality/limitations. Complete full
validation and obtain independent review before an authorized maintainer merges the PR. Release
preparation itself must stop for review without merging, tagging, publishing, or beginning v0.3/v0.4.

Only after that approved PR is merged and a maintainer explicitly authorizes publication should
the release operator confirm the exact approved `main` commit, check that `v0.2.0` does not already
exist, and create an annotated `v0.2.0` tag pointing exactly to that commit. Push that tag, then
create the normal stable GitHub Release from the existing tag. If the tag already exists, inspect
it and do not recreate, move, overwrite, or delete it. Both `v0.1.0` and the future `v0.2.0` tag
are immutable.

Package-registry publication requires separate explicit authorization and a separately configured
publishing workflow; this repository's CI does not publish packages. Tagging or creating a GitHub
Release does not authorize PyPI or any other registry publication.
