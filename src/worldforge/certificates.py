"""Validated, self-contained Failure Certificates and safe local persistence.

Certificates describe observed simulation failures. They are evidence, not signed
attestations, and never execute the stored agent configuration when loaded.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import JsonValue

from worldforge.models import (
    EvaluationResult,
    FailureCertificate,
    FailureCriterion,
    FailureSpec,
    SearchEvent,
    WorldDefinition,
)
from worldforge.simulation import Simulator

MAX_CERTIFICATE_BYTES = 32 * 1024 * 1024
_CERTIFICATE_ID = re.compile(r"WF-F-[0-9a-f]{32}\Z")
_RUN_ID = re.compile(r"WF-[0-9a-f]{32}\Z")
FailureClassification = Literal[
    "unsafe_retry",
    "constraint_violation",
    "objective_failure",
    "excessive_retries",
    "execution_error",
]


def failure_classification(
    result: EvaluationResult,
    criterion: FailureCriterion,
) -> FailureClassification:
    """Derive the category from observable evaluation, in documented priority order."""
    if "unsafe_retry_key_changed" in result.constraint_violations:
        return "unsafe_retry"
    if result.constraint_violations:
        return "constraint_violation"
    if criterion.kind == "excessive_retries":
        return "excessive_retries"
    if not result.objective_completed:
        return "objective_failure"
    return "execution_error"


def failure_severity(
    classification: FailureClassification,
) -> tuple[Literal["HIGH", "MEDIUM"], str]:
    if classification in ("unsafe_retry", "constraint_violation"):
        return (
            "HIGH",
            "Heuristic HIGH: observed unsafe retry or explicit invariant/constraint violation.",
        )
    return "MEDIUM", "Heuristic MEDIUM: objective, retry budget, or execution failure."


def recorded_agent_timeout(configuration: dict[str, JsonValue]) -> float:
    """Reject malformed stored numbers before conversion, including enormous integers."""
    timeout = configuration.get("agent_timeout")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not 0 < timeout <= 60
        or not math.isfinite(timeout)
    ):
        raise ValueError(
            "certificate agent_timeout must be finite, positive, and at most 60 seconds"
        )
    return float(timeout)


def _event_conditions(event: SearchEvent) -> list[FailureSpec]:
    values = event.payload.get("condition_specs")
    if not isinstance(values, list) or len(values) > 8:
        raise ValueError("certificate phase event must contain bounded condition_specs")
    return [FailureSpec.model_validate(value) for value in values]


def _condition_keys(conditions: list[FailureSpec]) -> list[str]:
    return [condition.model_dump_json() for condition in conditions]


def _validate_search_evidence(certificate: FailureCertificate) -> None:
    """Check the executed-run ledger and replay the deletion-minimization proof."""
    events = certificate.events
    if events[0].type != "SEARCH_STARTED":
        raise ValueError("certificate search events must begin with SEARCH_STARTED")
    # Every execution occurs once; cached minimization references an earlier run
    # with the same seed/conditions/outcome and does not consume another run.
    ledger: dict[str, tuple[int, list[str], bool]] = {}
    phases = {"candidate": 0, "minimization": 0, "verification": 0}
    discovered = False
    verification_started = False
    created = False
    current = _condition_keys(certificate.original_conditions)
    failed_deletions: set[str] = set()
    for event in events[1:-1]:
        if event.type in {"CANDIDATE_TESTED", "MINIMIZATION_STEP", "VERIFICATION_RUN"}:
            run_id = event.payload.get("run_id")
            seed = event.payload.get("seed")
            qualified = event.payload.get("qualified")
            if (
                not isinstance(run_id, str)
                or run_id not in certificate.run_ids
                or isinstance(seed, bool)
                or not isinstance(seed, int)
                or not isinstance(qualified, bool)
            ):
                raise ValueError("certificate phase event has an invalid run/seed/outcome link")
            conditions = _condition_keys(_event_conditions(event))
            if event.type == "CANDIDATE_TESTED":
                if discovered or created or run_id in ledger:
                    raise ValueError("certificate candidate events have invalid phase/run ordering")
                phases["candidate"] += 1
            elif event.type == "MINIMIZATION_STEP":
                if not discovered or verification_started or created:
                    raise ValueError("certificate minimization event has invalid phase ordering")
                if seed != certificate.scenario.seed:
                    raise ValueError(
                        "certificate minimization seed differs from discovered failure"
                    )
                removed = FailureSpec.model_validate(event.payload.get("removed_condition"))
                if (
                    event.payload.get("removed_kind") != removed.kind
                    or event.payload.get("removed_action") != removed.action
                ):
                    raise ValueError("certificate minimization removal metadata is inconsistent")
                removed_key = removed.model_dump_json()
                if removed_key not in current:
                    raise ValueError("certificate minimization removed an absent condition")
                trial = current.copy()
                trial.remove(removed_key)
                if conditions != trial:
                    raise ValueError(
                        "certificate minimization trial does not match condition deletion"
                    )
                cached = event.payload.get("cached")
                if not isinstance(cached, bool):
                    raise ValueError("certificate minimization cache status must be explicit")
                if cached:
                    if ledger.get(run_id) != (seed, conditions, qualified):
                        raise ValueError(
                            "cached minimization event lacks matching earlier evidence"
                        )
                else:
                    if run_id in ledger:
                        raise ValueError("noncached minimization event must reference a fresh run")
                    phases["minimization"] += 1
                if qualified:
                    current = trial
                    failed_deletions.clear()
                else:
                    failed_deletions.add(removed_key)
            else:
                if not discovered or created or run_id in ledger:
                    raise ValueError("certificate verification events must reference fresh runs")
                verification_started = True
                if (
                    seed != certificate.scenario.seed
                    or conditions != _condition_keys(certificate.minimized_conditions)
                    or qualified is not True
                ):
                    raise ValueError(
                        "certificate verification event differs from minimized failure"
                    )
                phases["verification"] += 1
            ledger.setdefault(run_id, (seed, conditions, qualified))
        elif event.type == "FAILURE_DISCOVERED":
            run_id = event.payload.get("run_id")
            if (
                discovered
                or not isinstance(run_id, str)
                or ledger.get(run_id) != (certificate.scenario.seed, current, True)
            ):
                raise ValueError("certificate discovery event lacks qualifying candidate evidence")
            discovered = True
        elif event.type == "CERTIFICATE_CREATED":
            if (
                not verification_started
                or created
                or event.payload.get("certificate_id") != certificate.certificate_id
            ):
                raise ValueError("certificate creation event has an invalid state/identifier")
            created = True
        else:
            raise ValueError("certificate search contains an unexpected phase event")
    if not created or list(ledger) != certificate.run_ids:
        raise ValueError("certificate phase run ledger does not match recorded run IDs")
    audit = certificate.audit
    if phases != {
        "candidate": audit.candidate_runs,
        "minimization": audit.minimization_runs,
        "verification": audit.verification_runs,
    }:
        raise ValueError("certificate phase event counts do not match its run audit")
    if current != _condition_keys(certificate.minimized_conditions):
        raise ValueError(
            "certificate minimization evidence does not reach the minimized conditions"
        )
    if audit.one_minimal and failed_deletions != set(current):
        raise ValueError("certificate one-minimal claim lacks unsuccessful deletion evidence")


def world_hash(world: WorldDefinition) -> str:
    """Hash the complete declared World IR using canonical, finite JSON."""
    canonical = json.dumps(
        world.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def validate_certificate(certificate: FailureCertificate) -> FailureCertificate:
    """Revalidate mutable containers and evidence links before storage/execution."""
    certificate = FailureCertificate.model_validate(certificate.model_dump(mode="python"))
    if world_hash(certificate.world) != certificate.world_hash:
        raise ValueError("certificate world hash does not match the embedded World IR")
    originals = [item.model_dump_json() for item in certificate.original_conditions]
    minimized = [item.model_dump_json() for item in certificate.minimized_conditions]
    if len(originals) != len(set(originals)) or len(minimized) != len(set(minimized)):
        raise ValueError("certificate conditions must be unique")
    if not set(minimized).issubset(originals):
        raise ValueError("minimized conditions must be a subset of original conditions")
    # Both pools are part of the evidence contract, even though reproduction only
    # applies the minimized set. Reject impossible original actions/combination.
    Simulator(
        certificate.world, certificate.scenario.initial_state, certificate.original_conditions
    )
    Simulator(
        certificate.world, certificate.scenario.initial_state, certificate.minimized_conditions
    )
    recorded_agent_timeout(certificate.agent_configuration)
    if len(set(certificate.run_ids)) != len(certificate.run_ids) or any(
        not _RUN_ID.fullmatch(run_id) for run_id in certificate.run_ids
    ):
        raise ValueError("certificate run IDs must be valid and unique")
    audit = certificate.audit
    if audit.total_runs != (
        audit.candidate_runs + audit.minimization_runs + audit.verification_runs
    ) or audit.total_runs != len(certificate.run_ids):
        raise ValueError("certificate run audit does not match recorded run IDs")
    if (
        certificate.reproduction_count != audit.verification_runs
        or certificate.successful_reproduction_count != certificate.reproduction_count
    ):
        raise ValueError("a certified failure requires all recorded verification runs to qualify")
    if [event.sequence for event in certificate.events] != list(range(len(certificate.events))):
        raise ValueError("certificate search event sequence is not contiguous")
    if not certificate.events or certificate.events[-1].type != "SEARCH_COMPLETED":
        raise ValueError("certificate must contain a completed search audit")
    result = certificate.evaluation
    criterion = certificate.criterion
    retries = result.usage.retries
    if certificate.resource_report is not None:
        measurement = certificate.resource_report.metrics.get("retries")
        if measurement is not None and isinstance(measurement.value, int):
            retries = measurement.value
    qualifications = {
        "unsafe_retry": "unsafe_retry_key_changed" in result.constraint_violations,
        "objective_failure": not result.objective_completed,
        "constraint_violation": bool(result.constraint_violations),
        "excessive_retries": retries > criterion.retry_limit,
        "any_failure": not result.success,
    }
    if not qualifications[criterion.kind]:
        raise ValueError("certificate evaluation does not meet its configured failure criterion")
    classification = failure_classification(result, criterion)
    severity, severity_basis = failure_severity(classification)
    if (
        certificate.classification != classification
        or certificate.severity != severity
        or certificate.severity_basis != severity_basis
    ):
        raise ValueError("certificate classification/severity must match its observable evaluation")
    verification = [event for event in certificate.events if event.type == "VERIFICATION_RUN"]
    if len(verification) != certificate.reproduction_count or any(
        event.payload.get("qualified") is not True for event in verification
    ):
        raise ValueError("certificate verification events do not support its reproduction counts")
    if (
        certificate.events[-1].payload.get("status") != "certified"
        or certificate.events[-1].payload.get("certificate_id") != certificate.certificate_id
    ):
        raise ValueError("certificate completion event does not match its identifier/state")
    _validate_search_evidence(certificate)
    return certificate


class CertificateStore:
    """Atomic non-overwriting JSON storage; IDs are names, never paths."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def _path(self, certificate_id: str) -> Path:
        if not _CERTIFICATE_ID.fullmatch(certificate_id):
            raise ValueError("certificate ID must be WF-F- followed by 32 lowercase hex digits")
        # Reject a redirected store as well as redirected files. The directory is
        # explicitly supplied by the user; no paths come from certificate contents.
        if self.directory.is_symlink():
            raise ValueError("certificate store directory must not be a symlink")
        return self.directory / f"{certificate_id}.json"

    def save(self, certificate: FailureCertificate) -> Path:
        certificate = validate_certificate(certificate)
        destination = self._path(certificate.certificate_id)
        payload = certificate.model_dump_json(by_alias=True, indent=2).encode("utf-8")
        if len(payload) > MAX_CERTIFICATE_BYTES:
            raise ValueError("certificate artifact exceeds the 32 MiB limit")
        self.directory.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            prefix=".certificate-", suffix=".tmp", dir=self.directory
        )
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, destination)
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    def load(self, certificate_id: str) -> FailureCertificate:
        source = self._path(certificate_id)
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            os.close(descriptor)
            raise ValueError("certificate artifact must be a regular file")
        if file_stat.st_size > MAX_CERTIFICATE_BYTES:
            os.close(descriptor)
            raise ValueError("certificate artifact exceeds the 32 MiB limit")
        with os.fdopen(descriptor, "rb") as stream:
            payload = stream.read(MAX_CERTIFICATE_BYTES + 1)
        if len(payload) > MAX_CERTIFICATE_BYTES:
            raise ValueError("certificate artifact exceeds the 32 MiB limit")
        certificate = validate_certificate(FailureCertificate.model_validate_json(payload))
        if certificate.certificate_id != certificate_id:
            raise ValueError("stored certificate identifier does not match its filename")
        return certificate
