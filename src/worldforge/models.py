"""Versioned JSON-compatible contracts shared by compiler, runtime, and replay."""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, serialize_by_alias=True)


class Origin(StrEnum):
    DECLARED = "declared"
    INFERRED = "inferred"


class Provenance(Contract):
    origin: Origin
    source: str
    reason: str


class WorldResource(Contract):
    name: str
    schema_: dict[str, JsonValue] = Field(alias="schema")
    provenance: Provenance


class Relationship(Contract):
    resource: str
    field: str
    target_resource: str
    provenance: Provenance


class WorldAction(Contract):
    operation_id: str
    method: str
    path: str
    kind: Literal["create", "get", "list", "refund", "unsupported"]
    resource: str | None = None
    input_schema: dict[str, JsonValue] = Field(default_factory=dict)
    response_schemas: dict[str, dict[str, JsonValue]] = Field(default_factory=dict)
    provenance: Provenance
    supported: bool = True
    reason: str | None = None


class CompatibilityIssue(Contract):
    location: str
    severity: Literal["warning", "unsupported"]
    reason: str


class WorldDefinition(Contract):
    schema_version: Literal["1.0", "1.1"] = "1.0"
    world_id: str = Field(min_length=1)
    version: str = "1"
    title: str
    resources: dict[str, WorldResource]
    actions: dict[str, WorldAction]
    relationships: list[Relationship] = Field(default_factory=list)
    rules: list[str] = Field(default_factory=list)
    issues: list[CompatibilityIssue] = Field(default_factory=list)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def coherent_keys(self) -> WorldDefinition:
        for key, resource in self.resources.items():
            if key != resource.name:
                raise ValueError("resource key must equal resource name")
        for key, action in self.actions.items():
            if key != action.operation_id:
                raise ValueError("action key must equal operation_id")
            if action.supported and action.resource not in self.resources:
                raise ValueError(f"action {key} references an unknown resource")
            if action.supported and action.kind == "unsupported":
                raise ValueError("unsupported actions cannot be executable")
        for relationship in self.relationships:
            if (
                relationship.resource not in self.resources
                or relationship.target_resource not in self.resources
            ):
                raise ValueError("relationship references an unknown resource")
        return self


class WorldState(Contract):
    resources: dict[str, dict[str, dict[str, JsonValue]]] = Field(default_factory=dict)
    step: int = Field(default=0, ge=0)


class ToolResult(Contract):
    status: int = Field(ge=100, le=599)
    data: JsonValue = None
    error: str | None = None


class AgentAction(Contract):
    tool: str = Field(min_length=1)
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


class AgentDecision(Contract):
    action: AgentAction | None = None
    message: str | None = None


class FailureInjection(Contract):
    action: str
    kind: Literal["error_before", "response_lost"]
    occurrence: int = Field(default=1, ge=1)


class FailureSpec(Contract):
    """Versioned deterministic synthetic fault; latency is virtual, never a sleep."""

    schema_version: Literal["1.1"] = "1.1"
    action: str = Field(min_length=1, max_length=256)
    kind: Literal[
        "error_before",
        "response_lost",
        "delay",
        "timeout",
        "permission_denied",
        "rate_limit",
        "service_unavailable",
        "malformed_response",
        "stale_read",
        "duplicate_delivery",
    ]
    occurrence: int = Field(default=1, ge=1, le=1000, strict=True)
    latency_ms: int = Field(default=0, ge=0, le=60_000, strict=True)


class RefundObjective(Contract):
    kind: Literal["refund_duplicate"] = "refund_duplicate"
    customer_id: str
    original_payment_id: str
    duplicate_payment_id: str
    amount: int = Field(gt=0)


class Scenario(Contract):
    schema_version: Literal["1.0", "1.1"] = "1.0"
    scenario_id: str
    world_id: str
    world_version: str = "1"
    seed: int
    task: str
    visible: dict[str, JsonValue]
    initial_state: WorldState
    objective: RefundObjective
    allowed_actions: list[str]
    max_steps: int = Field(default=12, ge=1, le=1000)
    failures: list[FailureInjection | FailureSpec] = Field(default_factory=list, max_length=32)
    simulated_latency_budget_ms: int = Field(default=300_000, ge=0, le=3_600_000, strict=True)
    max_runtime_seconds: float = Field(default=30.0, gt=0, le=3600, allow_inf_nan=False)

    @model_validator(mode="after")
    def versioned_extensions(self) -> Scenario:
        if self.schema_version == "1.0" and (
            any(isinstance(failure, FailureSpec) for failure in self.failures)
            or self.simulated_latency_budget_ms != 300_000
            or self.max_runtime_seconds != 30.0
        ):
            raise ValueError("extended failures and budgets require scenario schema_version 1.1")
        return self

    @model_serializer(mode="wrap")
    def compatible_dump(self, handler: SerializerFunctionWrapHandler) -> dict[str, JsonValue]:
        data: dict[str, JsonValue] = handler(self)
        if self.schema_version == "1.0":
            data.pop("simulated_latency_budget_ms", None)
            data.pop("max_runtime_seconds", None)
        return data


class AgentObservation(Contract):
    task: str
    visible: dict[str, JsonValue]
    tools: dict[str, WorldAction]
    last_result: ToolResult | None = None
    steps_remaining: int = Field(ge=0)


class WorldEvent(Contract):
    schema_version: Literal["1.0"] = "1.0"
    sequence: int = Field(ge=0)
    timestamp: str
    run_id: str
    scenario_id: str
    world_id: str
    world_version: str
    type: str
    payload: dict[str, JsonValue] = Field(default_factory=dict)


class ResourceUsage(Contract):
    counter_confidence: Literal["EXACT"] = "EXACT"
    tool_calls: int = Field(default=0, ge=0)
    world_steps: int = Field(default=0, ge=0)
    retries: int = Field(default=0, ge=0)
    failed_calls: int = Field(default=0, ge=0)
    elapsed_seconds: float = Field(default=0.0, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    cost_confidence: Literal["ESTIMATED"] | None = None

    @model_validator(mode="after")
    def label_estimated_cost(self) -> ResourceUsage:
        if (self.estimated_cost_usd is None) != (self.cost_confidence is None):
            raise ValueError("estimated cost and its ESTIMATED label must be supplied together")
        return self


class EvaluationResult(Contract):
    success: bool
    objective_completed: bool
    constraint_violations: list[str] = Field(default_factory=list)
    termination: str
    usage: ResourceUsage


class MeasurementProvenance(StrEnum):
    EXACT = "EXACT"
    CALCULATED = "CALCULATED"
    ESTIMATED = "ESTIMATED"
    UNAVAILABLE = "UNAVAILABLE"


class Measurement(Contract):
    value: int | float | None = None
    provenance: MeasurementProvenance
    source: str = Field(min_length=1, max_length=500)
    unit: str = Field(min_length=1, max_length=100)

    @field_validator("value", mode="before")
    @classmethod
    def real_numeric_value(cls, value: object) -> object:
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValueError("measurement value must be a number or unavailable")
        return value

    @model_validator(mode="after")
    def honest_measurement(self) -> Measurement:
        if (self.value is None) != (self.provenance == MeasurementProvenance.UNAVAILABLE):
            raise ValueError("missing measurement must have UNAVAILABLE provenance")
        if self.value is not None and (not math.isfinite(self.value) or self.value < 0):
            raise ValueError("measurement must be finite and nonnegative")
        return self


class ResourceWarning(Contract):
    code: str
    message: str


class ProviderIdentity(Contract):
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    kind: Literal["mock", "custom"] = "custom"


class RateLimitMetadata(Contract):
    limit: int | None = Field(default=None, ge=0, le=10**12, strict=True)
    remaining: int | None = Field(default=None, ge=0, le=10**12, strict=True)
    reset_after_ms: int | None = Field(default=None, ge=0, le=3_600_000, strict=True)

    @model_validator(mode="after")
    def coherent_limit(self) -> RateLimitMetadata:
        if self.limit is not None and self.remaining is not None and self.remaining > self.limit:
            raise ValueError("reported remaining quota cannot exceed its limit")
        return self


class ProviderUsage(Contract):
    requests: int | None = Field(default=None, ge=0, le=10**12, strict=True)
    input_tokens: int | None = Field(default=None, ge=0, le=10**12, strict=True)
    output_tokens: int | None = Field(default=None, ge=0, le=10**12, strict=True)
    rate_limit: RateLimitMetadata | None = None


class PricingConfig(Contract):
    input_usd_per_million: float = Field(ge=0, le=1_000_000, allow_inf_nan=False)
    output_usd_per_million: float = Field(ge=0, le=1_000_000, allow_inf_nan=False)


class ProviderRecord(Contract):
    identity: ProviderIdentity
    usage: ProviderUsage = Field(default_factory=ProviderUsage)
    pricing: PricingConfig | None = None
    request_id: str | None = Field(default=None, max_length=128)


class ProviderRequest(Contract):
    observation: AgentObservation
    request_metadata: dict[str, JsonValue] = Field(default_factory=dict, max_length=32)


class ProviderResponse(Contract):
    decision: AgentDecision
    record: ProviderRecord


class ResourceReport(Contract):
    schema_version: Literal["1.0"] = "1.0"
    metrics: dict[str, Measurement] = Field(default_factory=dict)
    warnings: list[ResourceWarning] = Field(default_factory=list)
    providers: list[ProviderRecord] = Field(default_factory=list, max_length=2000)


class Run(Contract):
    schema_version: Literal["1.0", "1.1"] = "1.1"
    run_id: str = Field(pattern=r"^WF-[0-9a-f]{32}$")
    created_at: str
    world: WorldDefinition
    scenario: Scenario
    agent_metadata: dict[str, JsonValue]
    events: list[WorldEvent]
    final_state: WorldState
    evaluation: EvaluationResult
    resource_report: ResourceReport | None = None
    certificate_id: str | None = Field(default=None, pattern=r"^WF-F-[0-9a-f]{32}$")

    @model_validator(mode="after")
    def versioned_report(self) -> Run:
        if self.schema_version == "1.0" and (
            self.resource_report is not None or self.certificate_id is not None
        ):
            raise ValueError(
                "resource reports and certificate linkage require run schema_version 1.1"
            )
        return self

    @model_serializer(mode="wrap")
    def compatible_dump(self, handler: SerializerFunctionWrapHandler) -> dict[str, JsonValue]:
        data: dict[str, JsonValue] = handler(self)
        if self.schema_version == "1.0":
            data.pop("resource_report", None)
            data.pop("certificate_id", None)
        return data


class FailureCriterion(Contract):
    kind: Literal[
        "unsafe_retry",
        "objective_failure",
        "constraint_violation",
        "excessive_retries",
        "any_failure",
    ] = "unsafe_retry"
    retry_limit: int = Field(default=3, ge=0, le=1000, strict=True)


class SearchSpec(Contract):
    schema_version: Literal["1.0"] = "1.0"
    family: Literal["payments_duplicate_charge"] = "payments_duplicate_charge"
    seeds: list[Annotated[int, Field(strict=True)]] = Field(
        default_factory=lambda: [42], min_length=1, max_length=32
    )
    conditions: list[FailureSpec] = Field(default_factory=list, max_length=8)
    criterion: FailureCriterion = Field(default_factory=FailureCriterion)
    max_candidates: int = Field(default=32, ge=1, le=256, strict=True)
    max_runs: int = Field(default=64, ge=1, le=512, strict=True)
    max_seconds: float = Field(default=30.0, ge=0.05, le=3600, allow_inf_nan=False)
    verification_runs: int = Field(default=3, ge=2, le=20, strict=True)
    agent_timeout: float = Field(default=2.0, ge=0.01, le=60, allow_inf_nan=False)

    @model_validator(mode="after")
    def bounded_unique_inputs(self) -> SearchSpec:
        if len(self.seeds) != len(set(self.seeds)):
            raise ValueError("search seeds must be unique")
        signatures = [(item.action, item.kind, item.occurrence) for item in self.conditions]
        if len(signatures) != len(set(signatures)):
            raise ValueError("search conditions must be unique")
        return self


class SearchAudit(Contract):
    candidate_runs: int = Field(default=0, ge=0)
    minimization_runs: int = Field(default=0, ge=0)
    verification_runs: int = Field(default=0, ge=0)
    total_runs: int = Field(default=0, ge=0)
    budget_exhausted: bool = False
    one_minimal: bool = False


class SearchEvent(Contract):
    sequence: int = Field(ge=0)
    type: Literal[
        "SEARCH_STARTED",
        "CANDIDATE_TESTED",
        "FAILURE_DISCOVERED",
        "MINIMIZATION_STEP",
        "VERIFICATION_RUN",
        "CERTIFICATE_CREATED",
        "SEARCH_COMPLETED",
    ]
    payload: dict[str, JsonValue] = Field(default_factory=dict)


class FailureCertificate(Contract):
    schema_version: Literal["1.0"] = "1.0"
    certificate_id: str = Field(pattern=r"^WF-F-[0-9a-f]{32}$")
    created_at: str
    world: WorldDefinition
    world_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    scenario: Scenario
    agent_metadata: dict[str, JsonValue]
    agent_configuration: dict[str, JsonValue]
    criterion: FailureCriterion
    classification: Literal[
        "unsafe_retry",
        "constraint_violation",
        "objective_failure",
        "excessive_retries",
        "execution_error",
    ]
    severity: Literal["HIGH", "MEDIUM", "LOW"]
    severity_basis: str
    original_conditions: list[FailureSpec] = Field(max_length=8)
    minimized_conditions: list[FailureSpec] = Field(max_length=8)
    reproduction_count: int = Field(ge=2, le=20)
    successful_reproduction_count: int = Field(ge=0, le=20)
    reproduction_rate: float = Field(ge=0, le=1, allow_inf_nan=False)
    evaluation: EvaluationResult
    resource_report: ResourceReport | None = None
    run_ids: list[str] = Field(min_length=1, max_length=512)
    audit: SearchAudit
    events: list[SearchEvent]

    @model_validator(mode="after")
    def coherent_certificate(self) -> FailureCertificate:
        if self.successful_reproduction_count > self.reproduction_count:
            raise ValueError("successful reproductions exceed attempts")
        if self.reproduction_rate != self.successful_reproduction_count / self.reproduction_count:
            raise ValueError("reproduction rate must equal observed successes / attempts")
        if (
            self.scenario.world_id != self.world.world_id
            or self.scenario.world_version != self.world.version
        ):
            raise ValueError("certificate scenario and world must agree")
        if self.scenario.failures != self.minimized_conditions:
            raise ValueError("certificate scenario must contain the minimized conditions")
        return self


class SearchOutcome(Contract):
    status: Literal["certified", "exhausted", "unverified_failure"]
    certificate: FailureCertificate | None = None
    runs: list[Run]
    audit: SearchAudit
    events: list[SearchEvent]
