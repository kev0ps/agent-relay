"""Provider-neutral, closed descriptors and invocation metadata."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .json_bounds import (
    JsonObject,
    validate_json_bounds,
    validate_json_schema,
)

MAX_PROVIDER_NAME_LENGTH = 128
MAX_PROVIDER_PUBLIC_NAME_LENGTH = 128
MAX_PROVIDER_DESCRIPTION_LENGTH = 2048
MAX_PROVIDER_TOOLS = 128

ProviderRiskClass = Literal[
    "read_only",
    "interaction",
    "destructive",
    "admin",
    "blocked",
]
ProviderToolRisk = ProviderRiskClass

ProviderName = Annotated[
    str,
    Field(
        min_length=1,
        max_length=MAX_PROVIDER_NAME_LENGTH,
        pattern=r"^[A-Za-z0-9._-]+$",
    ),
]
ProviderToolName = Annotated[
    str,
    Field(
        min_length=1,
        max_length=MAX_PROVIDER_NAME_LENGTH,
        pattern=r"^[A-Za-z0-9._:-]+$",
    ),
]
ProviderPublicName = Annotated[
    str,
    Field(
        min_length=1,
        max_length=MAX_PROVIDER_PUBLIC_NAME_LENGTH,
        pattern=r"^[A-Za-z0-9._:-]+$",
    ),
]
ProviderDescription = Annotated[
    str,
    Field(min_length=1, max_length=MAX_PROVIDER_DESCRIPTION_LENGTH),
]


class _ProviderModel(BaseModel):
    """Closed, strict base for provider-neutral envelopes."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        populate_by_name=True,
        hide_input_in_errors=True,
    )


class ProviderToolDescriptor(_ProviderModel):
    """A bounded provider capability description, not an executable callback."""

    provider_name: ProviderName = Field(
        validation_alias=AliasChoices(
            "provider_name", "provider", "internal_provider_name"
        )
    )
    tool_name: ProviderToolName = Field(
        validation_alias=AliasChoices(
            "tool_name", "toolName", "internal_tool_name", "provider_tool_name"
        )
    )
    public_name: ProviderPublicName = Field(
        validation_alias=AliasChoices("name", "public_name", "publicName"),
        serialization_alias="name",
    )
    description: ProviderDescription
    input_schema: JsonObject = Field(
        validation_alias=AliasChoices("inputSchema", "input_schema"),
        serialization_alias="inputSchema",
    )
    output_schema: JsonObject | None = Field(
        default=None,
        validation_alias=AliasChoices("outputSchema", "output_schema"),
        serialization_alias="outputSchema",
    )
    annotations: JsonObject = Field(default_factory=dict)
    risk: ProviderRiskClass = Field(
        validation_alias=AliasChoices("risk", "risk_class"),
        serialization_alias="risk",
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_name_alias(cls, value: object) -> object:
        return _normalize_name_alias(value)

    @field_validator("input_schema", "output_schema", mode="before")
    @classmethod
    def _bounded_schema(cls, value: object) -> object:
        if value is None:
            return None
        return validate_json_schema(value)

    @field_validator("annotations", mode="before")
    @classmethod
    def _bounded_annotations(cls, value: object) -> object:
        if value is None:
            raise ValueError("annotations must be a JSON object")
        return validate_json_bounds(
            value,
            require_object=True,
            label="annotations",
            reject_unsafe_metadata=True,
        )

    @model_validator(mode="after")
    def _bounded_descriptor(self) -> "ProviderToolDescriptor":
        validate_json_bounds(
            self.model_dump(mode="json", by_alias=True, exclude_none=True),
            require_object=True,
            label="descriptor",
        )
        return self

    @property
    def risk_class(self) -> ProviderRiskClass:
        """Compatibility view of the Task 1 ``risk`` contract field."""
        return self.risk

    @property
    def name(self) -> str:
        """MCP's public name spelling, while retaining the explicit field name."""
        return self.public_name


class ProviderToolInvocation(_ProviderModel):
    """A bounded provider/internal/public-name invocation envelope."""

    provider_name: ProviderName = Field(
        validation_alias=AliasChoices(
            "provider_name", "provider", "internal_provider_name"
        )
    )
    tool_name: ProviderToolName = Field(
        validation_alias=AliasChoices(
            "tool_name", "toolName", "internal_tool_name", "provider_tool_name"
        )
    )

    public_name: ProviderPublicName = Field(
        validation_alias=AliasChoices("public_name", "publicName"),
        serialization_alias="name",
    )
    arguments: JsonObject = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _normalize_name_alias(cls, value: object) -> object:
        return _normalize_name_alias(value)

    @field_validator("arguments", mode="before")
    @classmethod
    def _bounded_arguments(cls, value: object) -> object:
        return validate_json_bounds(
            value,
            require_object=True,
            label="arguments",
            reject_unsafe_metadata=True,
        )

    @model_validator(mode="after")
    def _bounded_invocation(self) -> "ProviderToolInvocation":
        validate_json_bounds(
            self.model_dump(mode="json", by_alias=True),
            require_object=True,
            label="invocation",
        )
        return self

    @property
    def name(self) -> str:
        return self.public_name


class ProviderToolCatalog(_ProviderModel):
    """A bounded closed collection of provider descriptors."""

    tools: list[ProviderToolDescriptor] = Field(
        default_factory=list,
        max_length=MAX_PROVIDER_TOOLS,
    )

    @model_validator(mode="after")
    def _reject_duplicate_names(self) -> "ProviderToolCatalog":
        internal_names: set[tuple[str, str]] = set()
        public_names: set[str] = set()
        for tool in self.tools:
            internal_name = (tool.provider_name, tool.tool_name)
            if internal_name in internal_names:
                raise ValueError(f"duplicate internal tool name: {tool.tool_name}")
            if tool.public_name in public_names:
                raise ValueError(f"duplicate public tool name: {tool.public_name}")
            internal_names.add(internal_name)
            public_names.add(tool.public_name)
        return self

    @model_validator(mode="after")
    def _bounded_catalog(self) -> "ProviderToolCatalog":
        validate_json_bounds(
            self.model_dump(mode="json", by_alias=True, exclude_none=True),
            require_object=True,
            label="catalog",
        )
        return self


def _normalize_name_alias(value: object) -> object:
    """Retain legacy ``name`` input without conflating the three identities."""
    if not isinstance(value, dict) or "name" not in value:
        return value

    normalized = dict(value)
    legacy_name = normalized.pop("name")
    has_public_name = any(key in normalized for key in ("public_name", "publicName"))
    has_tool_name = any(
        key in normalized
        for key in (
            "tool_name",
            "toolName",
            "internal_tool_name",
            "provider_tool_name",
        )
    )
    if has_public_name and has_tool_name:
        raise ValueError("name alias is ambiguous with public_name and tool_name")
    if has_public_name:
        normalized["tool_name"] = legacy_name
    else:
        normalized["public_name"] = legacy_name
    return normalized


# Short aliases are useful to adapters while keeping the descriptive public names.
ToolDescriptor = ProviderToolDescriptor
ToolInvocation = ProviderToolInvocation
ProviderToolCall = ProviderToolInvocation
ProviderToolDescriptors = ProviderToolCatalog

__all__ = [
    "MAX_PROVIDER_DESCRIPTION_LENGTH",
    "MAX_PROVIDER_NAME_LENGTH",
    "MAX_PROVIDER_PUBLIC_NAME_LENGTH",
    "MAX_PROVIDER_TOOLS",
    "ProviderDescription",
    "ProviderName",
    "ProviderPublicName",
    "ProviderToolName",
    "ProviderRiskClass",
    "ProviderToolCall",
    "ProviderToolCatalog",
    "ProviderToolDescriptor",
    "ProviderToolDescriptors",
    "ProviderToolInvocation",
    "ProviderToolRisk",
    "ToolDescriptor",
    "ToolInvocation",
]
