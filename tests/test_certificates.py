from __future__ import annotations

import json
from pathlib import Path

import pytest

from worldforge.agents import VulnerableRefundAgent
from worldforge.certificates import CertificateStore, validate_certificate, world_hash
from worldforge.compiler import compile_openapi
from worldforge.models import FailureCertificate, FailureSpec
from worldforge.oracle import default_search_spec, falsify_sync, reproduce_certificate_sync

EXAMPLE = Path(__file__).parents[1] / "examples/payments/openapi.yaml"


@pytest.fixture
def certificate() -> FailureCertificate:
    world = compile_openapi(EXAMPLE)
    certificate = falsify_sync(world, default_search_spec(world), VulnerableRefundAgent).certificate
    assert certificate is not None
    return certificate


def test_certificate_roundtrip_and_atomic_nonoverwriting_store(
    certificate: FailureCertificate,
    tmp_path: Path,
) -> None:
    store = CertificateStore(tmp_path / "certificates")
    destination = store.save(certificate)
    assert destination.name == certificate.certificate_id + ".json"
    assert store.load(certificate.certificate_id) == certificate
    assert FailureCertificate.model_validate_json(destination.read_text()) == certificate
    with pytest.raises(FileExistsError):
        store.save(certificate)
    assert not list(destination.parent.glob(".certificate-*.tmp"))


def test_world_hash_is_canonical_and_covers_declared_ir(certificate: FailureCertificate) -> None:
    world = certificate.world.model_copy(deep=True)
    expected = world_hash(world)
    world.actions = dict(reversed(list(world.actions.items())))
    world.resources = dict(reversed(list(world.resources.items())))
    assert world_hash(world) == expected
    world.title += " changed"
    assert world_hash(world) != expected


def test_world_hash_tampering_rejected_before_execution(
    certificate: FailureCertificate,
    tmp_path: Path,
) -> None:
    store = CertificateStore(tmp_path)
    destination = store.save(certificate)
    payload = json.loads(destination.read_text())
    payload["world"]["title"] = "Tampered World"
    destination.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="world hash"):
        store.load(certificate.certificate_id)
    certificate.world.title = "Tampered World"

    def factory() -> VulnerableRefundAgent:
        raise AssertionError("tampered world must not execute an adapter")

    with pytest.raises(ValueError, match="world hash"):
        reproduce_certificate_sync(certificate, factory)


@pytest.mark.parametrize("identifier", ["../secret", "/etc/passwd", "WF-F-123", "WF-F-" + "A" * 32])
def test_certificate_identifiers_cannot_traverse_paths(tmp_path: Path, identifier: str) -> None:
    with pytest.raises(ValueError, match="certificate ID"):
        CertificateStore(tmp_path).load(identifier)


def test_symlink_artifact_is_refused(certificate: FailureCertificate, tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text(certificate.model_dump_json())
    directory = tmp_path / "certificates"
    directory.mkdir()
    (directory / (certificate.certificate_id + ".json")).symlink_to(outside)
    with pytest.raises(OSError):
        CertificateStore(directory).load(certificate.certificate_id)


def test_symlink_store_is_refused(certificate: FailureCertificate, tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        CertificateStore(link).save(certificate)


def test_nonregular_artifact_is_refused(certificate: FailureCertificate, tmp_path: Path) -> None:
    (tmp_path / (certificate.certificate_id + ".json")).mkdir()
    with pytest.raises(ValueError, match="regular file"):
        CertificateStore(tmp_path).load(certificate.certificate_id)


def test_wrong_filename_is_refused(certificate: FailureCertificate, tmp_path: Path) -> None:
    wrong = "WF-F-" + "0" * 32
    (tmp_path / (wrong + ".json")).write_text(certificate.model_dump_json())
    with pytest.raises(ValueError, match="filename"):
        CertificateStore(tmp_path).load(wrong)


def test_oversized_artifact_is_refused(certificate: FailureCertificate, tmp_path: Path) -> None:
    path = tmp_path / (certificate.certificate_id + ".json")
    with path.open("wb") as stream:
        stream.truncate(32 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="32 MiB"):
        CertificateStore(tmp_path).load(certificate.certificate_id)


def test_minimized_conditions_must_come_from_originals(certificate: FailureCertificate) -> None:
    fault = FailureSpec(action="create_refund", kind="timeout")
    certificate.minimized_conditions[:] = [fault]
    certificate.scenario.failures[:] = [fault]
    with pytest.raises(ValueError, match="subset"):
        validate_certificate(certificate)


def test_nested_run_id_tampering_is_revalidated(certificate: FailureCertificate) -> None:
    certificate.run_ids[0] = "../secret"
    with pytest.raises(ValueError, match="run IDs"):
        validate_certificate(certificate)


def test_run_audit_must_agree_with_evidence(certificate: FailureCertificate) -> None:
    certificate.audit.total_runs += 1
    with pytest.raises(ValueError, match="run audit"):
        validate_certificate(certificate)


def test_certificate_reproduction_statistics_cannot_claim_fake_precision(
    certificate: FailureCertificate,
) -> None:
    payload = certificate.model_dump(mode="json")
    payload["reproduction_rate"] = 0.99
    with pytest.raises(ValueError, match="reproduction rate"):
        FailureCertificate.model_validate(payload)


def test_event_sequence_must_be_contiguous(certificate: FailureCertificate) -> None:
    certificate.events[1].sequence = 99
    with pytest.raises(ValueError, match="event sequence"):
        validate_certificate(certificate)


def test_stored_evaluation_must_meet_configured_failure_criterion(
    certificate: FailureCertificate,
) -> None:
    certificate.evaluation.constraint_violations.clear()
    with pytest.raises(ValueError, match="failure criterion"):
        validate_certificate(certificate)


def test_verification_events_cannot_disagree_with_reproduction_counts(
    certificate: FailureCertificate,
) -> None:
    event = next(event for event in certificate.events if event.type == "VERIFICATION_RUN")
    event.payload["qualified"] = False
    with pytest.raises(ValueError, match="verification events"):
        validate_certificate(certificate)


def test_completion_event_must_identify_its_certificate(certificate: FailureCertificate) -> None:
    certificate.events[-1].payload["certificate_id"] = "WF-F-" + "0" * 32
    with pytest.raises(ValueError, match="completion event"):
        validate_certificate(certificate)


@pytest.mark.parametrize("binding", ["nonexistent", "duplicate", "candidate"])
def test_verification_run_links_must_be_distinct_fresh_recorded_runs(
    certificate: FailureCertificate,
    binding: str,
) -> None:
    verification = [event for event in certificate.events if event.type == "VERIFICATION_RUN"]
    if binding == "nonexistent":
        verification[0].payload["run_id"] = "WF-" + "0" * 32
    elif binding == "duplicate":
        verification[1].payload["run_id"] = verification[0].payload["run_id"]
    else:
        verification[0].payload["run_id"] = certificate.run_ids[0]
    with pytest.raises(ValueError, match="(run/seed/outcome|fresh runs)"):
        validate_certificate(certificate)


def test_deleted_minimization_events_cannot_preserve_audit_claims(
    certificate: FailureCertificate,
) -> None:
    certificate.events[:] = [
        event for event in certificate.events if event.type != "MINIMIZATION_STEP"
    ]
    for sequence, event in enumerate(certificate.events):
        event.sequence = sequence
    with pytest.raises(ValueError, match="phase run ledger"):
        validate_certificate(certificate)


def test_one_minimal_claim_requires_each_retained_deletion_proof(
    certificate: FailureCertificate,
) -> None:
    certificate.events[:] = [
        event
        for event in certificate.events
        if not (event.type == "MINIMIZATION_STEP" and event.payload["cached"] is True)
    ]
    for sequence, event in enumerate(certificate.events):
        event.sequence = sequence
    with pytest.raises(ValueError, match="one-minimal claim"):
        validate_certificate(certificate)


def test_cached_minimization_requires_matching_previously_observed_conditions(
    certificate: FailureCertificate,
) -> None:
    minimization = [event for event in certificate.events if event.type == "MINIMIZATION_STEP"]
    minimization[1].payload["run_id"] = minimization[0].payload["run_id"]
    with pytest.raises(ValueError, match="cached minimization"):
        validate_certificate(certificate)


def test_noncached_minimization_cannot_be_relabelled_as_a_cached_run(
    certificate: FailureCertificate,
) -> None:
    event = next(event for event in certificate.events if event.type == "MINIMIZATION_STEP")
    event.payload["cached"] = True
    with pytest.raises(ValueError, match="cached minimization"):
        validate_certificate(certificate)


def test_minimization_event_must_describe_actual_condition_deletion(
    certificate: FailureCertificate,
) -> None:
    event = next(event for event in certificate.events if event.type == "MINIMIZATION_STEP")
    event.payload["condition_specs"] = []
    with pytest.raises(ValueError, match="condition deletion"):
        validate_certificate(certificate)


@pytest.mark.parametrize(
    "field,value",
    [
        ("classification", "execution_error"),
        ("severity", "LOW"),
        ("severity_basis", "No risk"),
    ],
)
def test_classification_and_severity_are_derived_from_observable_evaluation(
    certificate: FailureCertificate,
    field: str,
    value: str,
) -> None:
    setattr(certificate, field, value)
    with pytest.raises(ValueError, match="classification/severity"):
        validate_certificate(certificate)


def test_original_conditions_must_also_be_simulator_compatible(
    certificate: FailureCertificate,
) -> None:
    certificate.original_conditions.append(FailureSpec(action="nonexistent", kind="timeout"))
    with pytest.raises(ValueError, match="unknown"):
        validate_certificate(certificate)


@pytest.mark.parametrize("timeout", [10**400, True, "2", None, -1, 0, 61, float("nan")])
def test_stored_timeout_is_validated_before_numeric_conversion(
    certificate: FailureCertificate,
    timeout: object,
) -> None:
    certificate.agent_configuration["agent_timeout"] = timeout  # type: ignore[assignment]
    with pytest.raises(ValueError, match="agent_timeout"):
        validate_certificate(certificate)
