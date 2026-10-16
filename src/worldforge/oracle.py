"""Bounded deterministic failure search, deletion minimization, and reproduction.

Search ordering and simulator effects are deterministic. Trusted adapters, host
latency, IDs, and creation timestamps need not be. Verification reports observed
successes, never a statistical guarantee about external model inference.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from itertools import combinations
from typing import Literal
from uuid import uuid4

from pydantic import JsonValue, TypeAdapter

from worldforge.agents import AgentAdapter
from worldforge.certificates import (
    failure_classification,
    failure_severity,
    validate_certificate,
    world_hash,
)
from worldforge.models import (
    FailureCertificate,
    FailureCriterion,
    FailureSpec,
    Run,
    Scenario,
    SearchAudit,
    SearchEvent,
    SearchOutcome,
    SearchSpec,
    WorldDefinition,
)
from worldforge.runtime import run_scenario
from worldforge.scenarios import generate_scenarios
from worldforge.simulation import Simulator

_CONFIGURATION = TypeAdapter(dict[str, JsonValue])
_MAX_CONFIGURATION_BYTES = 64 * 1024
SearchEventKind = Literal[
    "SEARCH_STARTED",
    "CANDIDATE_TESTED",
    "FAILURE_DISCOVERED",
    "MINIMIZATION_STEP",
    "VERIFICATION_RUN",
    "CERTIFICATE_CREATED",
    "SEARCH_COMPLETED",
]


def default_search_spec(world: WorldDefinition) -> SearchSpec:
    """A small honest refund adversary with one removable irrelevant condition."""
    lists = sorted(
        key
        for key, action in world.actions.items()
        if action.supported and action.kind == "list" and action.resource == "payment"
    )
    refunds = sorted(
        key
        for key, action in world.actions.items()
        if action.supported and action.kind == "refund" and action.resource == "refund"
    )
    if len(lists) != 1 or len(refunds) != 1:
        raise ValueError("default search requires one declared payments list and refund action")
    return SearchSpec(
        conditions=[
            FailureSpec(action=lists[0], kind="delay", latency_ms=250),
            FailureSpec(action=refunds[0], kind="response_lost"),
        ]
    )


def _retries(run: Run) -> int:
    if run.resource_report is not None:
        measurement = run.resource_report.metrics.get("retries")
        if measurement is not None and isinstance(measurement.value, int):
            return measurement.value
    return run.evaluation.usage.retries


def criterion_matches(run: Run, criterion: FailureCriterion) -> bool:
    """Evaluate only the explicitly selected, observable failure criterion."""
    result = run.evaluation
    if criterion.kind == "unsafe_retry":
        return "unsafe_retry_key_changed" in result.constraint_violations
    if criterion.kind == "objective_failure":
        return not result.objective_completed
    if criterion.kind == "constraint_violation":
        return bool(result.constraint_violations)
    if criterion.kind == "excessive_retries":
        return _retries(run) > criterion.retry_limit
    return not result.success


def _condition_sets(conditions: list[FailureSpec]) -> Iterator[list[FailureSpec]]:
    """Baseline, full set, then increasing-cardinality subsets, without duplicates."""
    seen: set[tuple[int, ...]] = set()
    order = [(), tuple(range(len(conditions)))]
    for indices in order:
        if indices not in seen:
            seen.add(indices)
            yield [conditions[index] for index in indices]
    for size in range(1, len(conditions)):
        for indices in combinations(range(len(conditions)), size):
            if indices not in seen:
                seen.add(indices)
                yield [conditions[index] for index in indices]


def _key(seed: int, conditions: list[FailureSpec]) -> str:
    return json.dumps(
        [seed, [condition.model_dump(mode="json") for condition in conditions]],
        sort_keys=True,
        separators=(",", ":"),
    )


def _scenario(base: Scenario, conditions: list[FailureSpec], fingerprint: str) -> Scenario:
    data = base.model_dump(mode="python")
    identity_data = base.model_dump(mode="json", exclude={"scenario_id", "failures"})
    identity = json.dumps(
        {
            "world_hash": fingerprint,
            "scenario": identity_data,
            "conditions": [condition.model_dump(mode="json") for condition in conditions],
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    data.update(
        schema_version="1.1",
        failures=conditions,
        scenario_id="SC-F-" + hashlib.sha256(identity).hexdigest()[:24],
    )
    return Scenario.model_validate(data)


class _Search:
    def __init__(self, spec: SearchSpec) -> None:
        self.spec = spec
        self.deadline = time.perf_counter() + spec.max_seconds
        self.audit = SearchAudit()
        self.runs: list[Run] = []
        self.events: list[SearchEvent] = []
        self.cache: dict[str, Run] = {}

    def emit(self, kind: SearchEventKind, payload: dict[str, JsonValue] | None = None) -> None:
        self.events.append(SearchEvent(sequence=len(self.events), type=kind, payload=payload or {}))

    def available(self, phase: str) -> bool:
        available = (
            self.audit.total_runs < self.spec.max_runs
            and time.perf_counter() < self.deadline - 0.001
            and (phase != "candidate" or self.audit.candidate_runs < self.spec.max_candidates)
        )
        if not available:
            self.audit.budget_exhausted = True
        return available

    async def execute(
        self,
        world: WorldDefinition,
        scenario: Scenario,
        agent_factory: Callable[[], AgentAdapter],
        phase: str,
    ) -> Run | None:
        if not self.available(phase):
            return None
        remaining = self.deadline - time.perf_counter()
        # Only start a complete run when its fixed runtime and shutdown budgets
        # fit. Changing a scenario's deadline for each candidate would manufacture
        # timeout failures and make a certificate describe different conditions.
        if scenario.max_runtime_seconds + self.spec.agent_timeout > remaining:
            self.audit.budget_exhausted = True
            return None
        run = await run_scenario(world, scenario, agent_factory(), self.spec.agent_timeout)
        self.runs.append(run)
        self.audit.total_runs += 1
        if phase == "candidate":
            self.audit.candidate_runs += 1
        elif phase == "minimization":
            self.audit.minimization_runs += 1
        else:
            self.audit.verification_runs += 1
        return run


async def falsify(
    world: WorldDefinition,
    spec: SearchSpec,
    agent_factory: Callable[[], AgentAdapter],
    agent_configuration: dict[str, JsonValue] | None = None,
) -> SearchOutcome:
    """Search a finite payments family and certify only fully verified failures.

    Deletion minimization reaches an observed one-minimal set when budgets permit:
    removing any retained condition did not qualify. It does not claim a globally
    minimal scenario or that external model generations are deterministic.
    """
    spec = SearchSpec.model_validate(spec.model_dump(mode="python"))
    world = WorldDefinition.model_validate(world.model_dump(mode="python"))
    configuration = _CONFIGURATION.validate_python(agent_configuration or {})
    if len(json.dumps(configuration, allow_nan=False).encode()) > _MAX_CONFIGURATION_BYTES:
        raise ValueError("agent configuration exceeds the 64 KiB limit")
    fingerprint = world_hash(world)
    bases = [generate_scenarios(world, count=1, seed=seed)[0] for seed in spec.seeds]
    for base in bases:
        base.schema_version = "1.1"
        # Fixed for every candidate, minimization trial, verification, and stored
        # reproduction scenario. Half the search deadline bounds one runtime.
        base.max_runtime_seconds = min(base.max_runtime_seconds, spec.max_seconds / 2)
    # Validate the entire pool before instantiating any trusted adapter.
    for base in bases:
        Simulator(world, base.initial_state, spec.conditions)
    search = _Search(spec)
    search.emit(
        "SEARCH_STARTED",
        {
            "family": spec.family,
            "max_runs": spec.max_runs,
            "max_candidates": spec.max_candidates,
            "criterion": spec.criterion.kind,
        },
    )
    found: Run | None = None
    original: list[FailureSpec] = []
    base_found: Scenario | None = None
    # Condition sets are outermost so all seeds receive the baseline first.
    for conditions in _condition_sets(spec.conditions):
        for base in bases:
            candidate = _scenario(base, conditions, fingerprint)
            run = await search.execute(world, candidate, agent_factory, "candidate")
            if run is None:
                break
            search.cache[_key(base.seed, conditions)] = run
            qualifies = criterion_matches(run, spec.criterion)
            search.emit(
                "CANDIDATE_TESTED",
                {
                    "run_id": run.run_id,
                    "seed": base.seed,
                    "conditions": len(conditions),
                    "condition_specs": [item.model_dump(mode="json") for item in conditions],
                    "qualified": qualifies,
                },
            )
            if qualifies:
                found, original, base_found = run, conditions, base
                search.emit("FAILURE_DISCOVERED", {"run_id": run.run_id})
                break
        if found is not None or search.audit.budget_exhausted:
            break
    if found is None or base_found is None:
        search.emit("SEARCH_COMPLETED", {"status": "exhausted", "failure_found": False})
        return SearchOutcome(
            status="exhausted", runs=search.runs, audit=search.audit, events=search.events
        )

    minimized = original.copy()
    index = 0
    while index < len(minimized):
        removed = minimized[index]
        trial = minimized[:index] + minimized[index + 1 :]
        signature = _key(base_found.seed, trial)
        cached = signature in search.cache
        run = search.cache.get(signature)
        if run is None:
            run = await search.execute(
                world, _scenario(base_found, trial, fingerprint), agent_factory, "minimization"
            )
            if run is None:
                break
            search.cache[signature] = run
        qualifies = criterion_matches(run, spec.criterion)
        search.emit(
            "MINIMIZATION_STEP",
            {
                "run_id": run.run_id,
                "removed_kind": removed.kind,
                "removed_action": removed.action,
                "removed_condition": removed.model_dump(mode="json"),
                "condition_specs": [item.model_dump(mode="json") for item in trial],
                "seed": base_found.seed,
                "qualified": qualifies,
                "cached": cached,
            },
        )
        if qualifies:
            minimized, found = trial, run
            index = 0
        else:
            index += 1
    search.audit.one_minimal = index == len(minimized)
    verified: list[Run] = []
    successes = 0
    for _ in range(spec.verification_runs):
        run = await search.execute(
            world, _scenario(base_found, minimized, fingerprint), agent_factory, "verification"
        )
        if run is None:
            break
        verified.append(run)
        qualifies = criterion_matches(run, spec.criterion)
        successes += int(qualifies)
        search.emit(
            "VERIFICATION_RUN",
            {
                "run_id": run.run_id,
                "qualified": qualifies,
                "seed": base_found.seed,
                "condition_specs": [item.model_dump(mode="json") for item in minimized],
            },
        )
    if len(verified) != spec.verification_runs or successes != spec.verification_runs:
        search.emit(
            "SEARCH_COMPLETED",
            {
                "status": "unverified_failure",
                "successful_reproductions": successes,
                "reproduction_attempts": len(verified),
            },
        )
        return SearchOutcome(
            status="unverified_failure", runs=search.runs, audit=search.audit, events=search.events
        )

    identifier = f"WF-F-{uuid4().hex}"
    classification = failure_classification(verified[0].evaluation, spec.criterion)
    severity, severity_basis = failure_severity(classification)
    search.emit("CERTIFICATE_CREATED", {"certificate_id": identifier})
    search.emit("SEARCH_COMPLETED", {"status": "certified", "certificate_id": identifier})
    certificate = validate_certificate(
        FailureCertificate(
            certificate_id=identifier,
            created_at=datetime.now(UTC).isoformat(),
            world=world,
            world_hash=fingerprint,
            scenario=_scenario(base_found, minimized, fingerprint),
            agent_metadata=verified[0].agent_metadata,
            agent_configuration={"adapter": configuration, "agent_timeout": spec.agent_timeout},
            criterion=spec.criterion,
            classification=classification,
            severity=severity,
            severity_basis=severity_basis,
            original_conditions=original,
            minimized_conditions=minimized,
            reproduction_count=len(verified),
            successful_reproduction_count=successes,
            reproduction_rate=successes / len(verified),
            evaluation=verified[0].evaluation,
            resource_report=verified[0].resource_report,
            run_ids=[run.run_id for run in search.runs],
            audit=search.audit,
            events=search.events,
        )
    )
    for run in search.runs:
        run.certificate_id = identifier
    return SearchOutcome(
        status="certified",
        certificate=certificate,
        runs=search.runs,
        audit=search.audit,
        events=search.events,
    )


def falsify_sync(
    world: WorldDefinition,
    spec: SearchSpec,
    agent_factory: Callable[[], AgentAdapter],
    agent_configuration: dict[str, JsonValue] | None = None,
) -> SearchOutcome:
    return asyncio.run(falsify(world, spec, agent_factory, agent_configuration))


async def reproduce_certificate(
    certificate: FailureCertificate,
    agent_factory: Callable[[], AgentAdapter],
    count: int = 3,
    agent_timeout: float = 2.0,
) -> list[Run]:
    """Run an explicitly selected trusted agent; never load one from stored paths.

    Each reproduction retains the certificate's scenario step/time/virtual-latency
    budgets. Return real runs so callers can report criterion_matches honestly.
    """
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 20:
        raise ValueError("reproduction count must be an integer between 1 and 20")
    if (
        isinstance(agent_timeout, bool)
        or not isinstance(agent_timeout, (int, float))
        or not 0 < agent_timeout <= 60
        or not math.isfinite(agent_timeout)
    ):
        raise ValueError("agent timeout must be positive, finite, and at most 60 seconds")
    certificate = validate_certificate(certificate)
    Simulator(
        certificate.world, certificate.scenario.initial_state, certificate.minimized_conditions
    )
    runs: list[Run] = []
    for _ in range(count):
        run = await run_scenario(
            certificate.world, certificate.scenario, agent_factory(), agent_timeout
        )
        run.certificate_id = certificate.certificate_id
        runs.append(run)
    return runs


def reproduce_certificate_sync(
    certificate: FailureCertificate,
    agent_factory: Callable[[], AgentAdapter],
    count: int = 3,
    agent_timeout: float = 2.0,
) -> list[Run]:
    return asyncio.run(reproduce_certificate(certificate, agent_factory, count, agent_timeout))
