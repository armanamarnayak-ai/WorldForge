"""Versioned JSON-compatible contracts shared by compiler, runtime, and replay."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


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
    schema_version: Literal["1.0"] = "1.0"
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


class RefundObjective(Contract):
    kind: Literal["refund_duplicate"] = "refund_duplicate"
    customer_id: str
    original_payment_id: str
    duplicate_payment_id: str
    amount: int = Field(gt=0)


class Scenario(Contract):
    schema_version: Literal["1.0"] = "1.0"
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
    failures: list[FailureInjection] = Field(default_factory=list)


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


class Run(Contract):
    schema_version: Literal["1.0"] = "1.0"
    run_id: str = Field(pattern=r"^WF-[0-9a-f]{32}$")
    created_at: str
    world: WorldDefinition
    scenario: Scenario
    agent_metadata: dict[str, JsonValue]
    events: list[WorldEvent]
    final_state: WorldState
    evaluation: EvaluationResult
