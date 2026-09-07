"""Typed request envelopes and constrained emergency-response schema."""

from __future__ import annotations

import copy
import json
from enum import Enum
from typing import Annotated, Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class RequestType(str, Enum):
    MISSION = "mission"
    EMERGENCY = "emergency"


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["system", "user", "assistant"]
    content: str


class BackendRequest(BaseModel):
    """Identity is attached before queueing and survives every async boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1)
    request_type: RequestType
    generation: int = Field(ge=0)
    attempt: int = Field(ge=1)
    model: str = Field(min_length=1)
    messages: tuple[ChatMessage, ...] = Field(min_length=1)
    response_schema: Mapping[str, Any] | None = None
    temperature: float = Field(ge=0.0, le=2.0)
    seed: int | None = Field(default=None, ge=0)
    num_predict: int | None = Field(default=None, ge=1, le=32768)


ActionValue = Annotated[
    float,
    Field(ge=-1.0, le=1.0, allow_inf_nan=False, strict=True),
]


class ActionPhase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # These are dm_control control channels (yaw/lift/extend per leg), not
    # independent anatomical joint-position targets.
    actuator_targets: tuple[ActionValue, ...] = Field(min_length=12, max_length=12)
    duration_steps: int = Field(gt=0, le=10000, strict=True)


class EmergencyPreset(BaseModel):
    """The only payload accepted as an emergency scheduling result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["one-shot"]
    skill_name: str = Field(min_length=1, max_length=128)
    actions: tuple[ActionPhase, ...] = Field(min_length=1, max_length=64)
    # A one-shot command has an explicit finite expiry. Free-text conditions
    # such as "all_actuator_targets_zero" were not executable v2 semantics.
    stop_condition: Literal["duration_steps_elapsed"]
    # No default: an exact response must state the one-shot loop semantics.
    max_cycles: None

    @field_validator("skill_name")
    @classmethod
    def no_blank_strings(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


_EMERGENCY_JSON_SCHEMA: dict[str, Any] = EmergencyPreset.model_json_schema()


def emergency_json_schema() -> dict[str, Any]:
    """Return a fresh JSON Schema object suitable for Ollama's ``format`` field."""

    return copy.deepcopy(_EMERGENCY_JSON_SCHEMA)


def parse_emergency_response(payload: str) -> EmergencyPreset:
    """Parse exact JSON and validate it with the same schema sent to the model."""

    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"response is not valid JSON: {exc.msg}") from exc
    try:
        return EmergencyPreset.model_validate(decoded)
    except ValidationError as exc:
        raise ValueError(f"response violates emergency schema: {exc}") from exc
