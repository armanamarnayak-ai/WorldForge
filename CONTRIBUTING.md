# Contributing to WorldForge

WorldForge v0.1 focuses on one complete OpenAPI-to-evaluation-and-replay workflow. Please discuss
large scope changes in an issue before implementing them; [ROADMAP.md](ROADMAP.md) distinguishes
the current foundation from later work. Follow our [Code of Conduct](CODE_OF_CONDUCT.md).

## Development setup

Use Python 3.11 or newer on Linux or macOS and the existing checkout. Windows is not currently
validated. Create a branch for each meaningful change;
do not develop directly on `main` or merge your own major changes without review.

```bash
git switch -c worldforge/my-change
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

The project uses a `src/` package layout, Pydantic v2 contracts, pytest, Hypothesis, Ruff,
strict mypy, and Hatchling builds. A local model
key or external application service is unnecessary for the reference workflow.

## Make a reviewable change

Keep changes focused, preserve versioned contracts, and document compatibility changes. Add
tests for behavior, failure paths, and important invariants. Keep simulator results independent
of wall-clock time and global randomness; do not invent business semantics or precision in
evaluation metrics. Document supported OpenAPI features as they change.

Before submitting, run:

```bash
ruff check .
ruff format --check .
mypy src/worldforge
pytest
python -m build
```

Run the [documented example](docs/getting-started.md) from a clean local workspace and inspect
`git diff` and `git status` for unintended files. Never commit `.venv`, generated run data,
credentials, or private observations. Formatting changes can be applied with `ruff format .`.
Use [release instructions](docs/releasing.md) to verify a clean wheel installation when changing
packaging, entry points, or examples. CI checks Python 3.11–3.13.

## Pull requests and issues

Describe the concrete problem and resulting behavior, architecture decisions, commands and
results of validation, known limitations, and manual reproduction steps. State the next milestone
when relevant. Use the issue and PR templates and distinguish observed results from expectations.
Do not weaken tests to obtain a passing build, add empty implementations for roadmap features,
or automatically merge a release-candidate PR.

For vulnerabilities, follow [SECURITY.md](SECURITY.md) rather than posting exploit details or
sensitive data in a public issue.
