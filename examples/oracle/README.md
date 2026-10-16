# Refund failure-discovery example

The local payments world and deterministic vulnerable policy require no network,
model provider, or credentials. Run from the repository checkout:

```sh
worldforge import examples/payments/openapi.yaml --workspace /tmp/worldforge-oracle-demo
worldforge falsify --agent vulnerable-reference \
  --search examples/oracle/refund-search.json --workspace /tmp/worldforge-oracle-demo
```

The search tests a clean baseline, then a 250 ms **simulated** list delay together
with a lost refund response. The refund succeeds before the response is lost.
The deliberately vulnerable agent changes its idempotency key while retrying.
WorldForge records `unsafe_retry_key_changed`; the simulator continues enforcing
refund conservation and rejects a second full refund. This demonstrates an unsafe
attempt, not an actual double refund.

Deletion minimization removes the irrelevant list delay, leaving one
`response_lost` condition. Three fresh runs must reproduce the criterion before
WorldForge creates a certificate. The default example uses six runs: two search
candidates, one new minimization trial, and three verification runs. An already
recorded clean baseline supplies the unsuccessful zero-condition trial.

Copy the printed certificate ID into the commands below, replacing `WF-F-...`:

```sh
worldforge failure inspect WF-F-... --workspace /tmp/worldforge-oracle-demo
worldforge failure reproduce WF-F-... --workspace /tmp/worldforge-oracle-demo
worldforge failure reproduce WF-F-... --agent reference --workspace /tmp/worldforge-oracle-demo
```

The vulnerable policy should reproduce 3/3. The safe reference policy reuses its
key, completes the objective, and reproduces 0/3; that comparison intentionally
returns exit status 1 because it did not reproduce the stored failure criterion.
Use each printed run ID with `worldforge replay RUN_ID --workspace ...` to verify
recorded simulator actions without regenerating agent outputs.

`refund-search.json` is a bounded, versioned search definition. Search explores
failure subsets in a fixed order and uses one shared candidate/run/time budget for
search, minimization, and verification. A scenario's runtime budget is fixed at
the smaller of its default runtime limit and half the search's wall-clock budget;
a new run starts only when its runtime and shutdown budgets fit the remaining
time. One-minimal means each remaining condition was individually tested for
removal, not that the result is globally optimal. Reproduction rates are observed
counts, and severity is a documented heuristic, not a probability or production
impact estimate. External model inference is not claimed to be deterministic.

The optional `examples/agents/vulnerable.py` exposes the same policy through the
trusted local Python adapter boundary. Loading it requires `--trust-agent-code`.
Python adapters run in process; they are not sandboxed. Stored adapter paths never
grant permission to execute code during certificate inspection or reproduction.
