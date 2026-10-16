# Security policy

WorldForge is experimental; v0.1.0 is released and v0.2 is in development. Security fixes target
the current development version; no older-release support commitment or audited sandbox is provided.

## Boundaries

- Simulator actions operate on per-run in-memory synthetic state. The simulator does not make
  production HTTP requests, execute OpenAPI server definitions, or contact MCP servers. The
  static MCP compiler preserves declarations as data and requires explicit synthetic bindings.
- The compiler uses safe YAML/JSON parsing and rejects remote/dynamic references and regex schema
  constraints. Documents and runtime work have implementation limits, but these controls are not
  a general denial-of-service guarantee. Direct IR validation remains offline, and searches bound
  conditions, seeds, candidates, total runs, verification attempts, and elapsed time.
- Python agent adapters are trusted executable code. The CLI requires `--trust-agent-code` for
  custom files; importing such a file can execute code with the process's filesystem, network,
  environment-variable, and subprocess permissions. Step limits and asynchronous timeouts do
  not contain malicious code or interrupt arbitrary blocking code.
- Run files are written locally and may contain agent-visible task data, tool arguments,
  responses, metadata, and state. There is no automatic telemetry upload or PII/secret scrubber.
  Inspect and sanitize files before sharing them. Avoid real customer data and credentials in
  synthetic inputs.
- Replay checks stored simulator behavior. It is not a cryptographic attestation of the run's
  author or an assertion of fidelity to a real service.
- Failure Certificates validate embedded world hashes and evidence links. Inspection never
  executes stored adapters; reproduction of custom Python requires explicit selection/trust.
  Severity is heuristic, verification rates are empirical, and external model inference can vary.

See [v0.2 security and replay notes](docs/v0.2-security-replay.md) for artifact, MCP, provider,
composition, and search-specific boundaries.

Use disposable credentials and a separate process/container with network and filesystem controls
when developing adapters with broader access. Do not run untrusted Python agents merely because
their tools are simulated. Keep dependencies updated and inspect imported specifications.

## Reporting a vulnerability

If GitHub private vulnerability reporting is enabled for this repository, use **Security → Report
a vulnerability** at
[the repository security page](https://github.com/armanamarnayak-ai/WorldForge/security).
Include the affected commit/version, impact, a minimal reproduction using synthetic data, and
any suggested fix. Do not include secret values or personal data.

If private reporting is unavailable, open a public issue requesting a private reporting channel
without publishing exploit instructions or sensitive details. Maintainers can arrange a channel
before receiving the report. No security contact address or response-time guarantee has been
established yet.
