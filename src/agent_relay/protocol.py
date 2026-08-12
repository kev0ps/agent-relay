"""Strict identity frames and provider-neutral v2 application messages."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from .json_bounds import (
    MAX_JSON_BYTES,
    MAX_JSON_DEPTH,
    MAX_JSON_NODES,
    JsonObject,
    validate_json_bounds,
)

MAX_TOKEN_LENGTH = 256
MAX_CAPABILITIES = 128
MAX_ERROR_MESSAGE_LENGTH = 512
MAX_PROGRESS_MESSAGE_LENGTH = 512
MAX_RESULT_JSON_BYTES = MAX_JSON_BYTES
MAX_RESULT_DEPTH = MAX_JSON_DEPTH
MAX_RESULT_NODES = MAX_JSON_NODES

RequestId = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
]
DeviceId = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
]
CommandId = Literal["pwd", "whoami", "python_version", "git_status", "git_branch"]
ToolName = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
]
TOOL_ORDER: tuple[str, ...] = (
    "system.ping",
    "terminal.exec",
)

# Imported after the bounded frame primitives to keep the provider result model
# independent from the protocol module's application-frame definitions.
from .output_models import ProviderToolResult  # noqa: E402
from .provider_tools import ProviderToolDescriptor  # noqa: E402


class Message(BaseModel):
    """Base class which rejects unknown wire fields."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    version: Literal[1]
    type: str


class Register(Message):
    type: Literal["register"]
    device_id: DeviceId


class Capabilities(Message):
    type: Literal["capabilities"]
    tools: list[ToolName] = Field(min_length=0, max_length=MAX_CAPABILITIES)
    descriptors: list[ProviderToolDescriptor] = Field(
        default_factory=list,
        max_length=MAX_CAPABILITIES,
    )

    @model_validator(mode="after")
    def descriptor_names_match_tools(self) -> "Capabilities":
        if not self.descriptors:
            return self
        descriptor_names = [
            f"{descriptor.provider_name}.{descriptor.tool_name}"
            for descriptor in self.descriptors
        ]
        if len(set(descriptor_names)) != len(descriptor_names):
            raise ValueError("duplicate capability descriptor")
        if set(self.tools) != set(descriptor_names):
            raise ValueError("capability tools do not match descriptors")
        return self


class ApplicationMessage(BaseModel):
    """Closed v2 frame used only after the Agent is authenticated."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    version: Literal[2]
    type: str


class Heartbeat(ApplicationMessage):
    type: Literal["heartbeat"]


class AgentResult(ApplicationMessage):
    type: Literal["result"]
    request_id: RequestId
    result: ProviderToolResult


class ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    code: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_.-]+$")]
    message: Annotated[str, Field(min_length=1, max_length=MAX_ERROR_MESSAGE_LENGTH)]


class AgentError(ApplicationMessage):
    type: Literal["error"]
    request_id: RequestId
    error: ErrorDetail


class Progress(ApplicationMessage):
    type: Literal["progress"]
    request_id: RequestId
    progress: Annotated[int, Field(ge=0, le=100)]
    message: Annotated[str, Field(max_length=MAX_PROGRESS_MESSAGE_LENGTH)] = ""


class Registered(Message):
    type: Literal["registered"]
    device_id: DeviceId


class InvokeMessage(ApplicationMessage):
    """Provider-neutral invocation with bounded opaque JSON arguments."""

    type: Literal["invoke"]
    request_id: RequestId
    tool_name: ToolName
    arguments: JsonObject = Field(default_factory=dict)

    @field_validator("arguments", mode="before")
    @classmethod
    def bounded_arguments(cls, value: object) -> object:
        return validate_json_bounds(value, require_object=True, label="arguments")


class Cancel(ApplicationMessage):
    type: Literal["cancel"]
    request_id: RequestId
    reason: Annotated[str, Field(min_length=1, max_length=256)]


AgentMessage = Register | Capabilities | Heartbeat | AgentResult | AgentError | Progress
ServerMessage = Registered | InvokeMessage | Cancel

_agent_adapter = TypeAdapter(Annotated[AgentMessage, Field(discriminator="type")])


def parse_agent_message(value: object) -> AgentMessage:
    """Parse one decoded JSON object from an agent."""
    if isinstance(value, dict) and value.get("type") in {"result", "error", "progress"}:
        if value.get("version") != 2:
            raise ValueError("invalid application message version")
    return _agent_adapter.validate_python(value)


def parse_server_message(value: object) -> ServerMessage:
    """Parse one decoded JSON object from the relay server."""
    if not isinstance(value, dict):
        raise ValueError("server message must be an object")
    message_type = value.get("type")
    if message_type == "registered":
        return Registered.model_validate(value)
    if message_type == "cancel":
        return Cancel.model_validate(value)
    if message_type == "invoke":
        return InvokeMessage.model_validate(value)
    raise ValueError("unknown server message")
