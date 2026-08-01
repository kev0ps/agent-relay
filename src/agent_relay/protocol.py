"""Strict, versioned JSON wire models for the Relay v1 protocol."""

from __future__ import annotations

import json
import unicodedata
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
)

MAX_TOKEN_LENGTH = 256
MAX_CAPABILITIES = 16
MAX_ERROR_MESSAGE_LENGTH = 512
MAX_PROGRESS_MESSAGE_LENGTH = 512
MAX_RESULT_JSON_BYTES = 64 * 1024
MAX_RESULT_DEPTH = 16
MAX_RESULT_NODES = 4096
MAX_BROWSER_URL_LENGTH = 2048
MAX_BROWSER_ELEMENT_ID_LENGTH = 128
MAX_BROWSER_FILL_VALUE_LENGTH = 4096
MAX_BROWSER_TYPE_TEXT_LENGTH = 4096
MAX_BROWSER_TAB_ID_LENGTH = 128
MAX_BROWSER_TITLE_LENGTH = 256
MAX_BROWSER_PAGE_TEXT_LENGTH = 4096
MAX_BROWSER_ROLE_LENGTH = 64
MAX_BROWSER_NAME_LENGTH = 128
MAX_BROWSER_ELEMENT_VALUE_LENGTH = 256
MAX_BROWSER_TABS = 6
MAX_BROWSER_ELEMENTS = 12
MAX_COMPUTER_ELEMENT_ID_LENGTH = 128
MAX_COMPUTER_TYPE_TEXT_LENGTH = 4096
MAX_COMPUTER_APP_LENGTH = 128
MAX_COMPUTER_WINDOW_TITLE_LENGTH = 256
MAX_COMPUTER_GENERATION_LENGTH = 128
MAX_COMPUTER_ROLE_LENGTH = 64
MAX_COMPUTER_NAME_LENGTH = 128
MAX_COMPUTER_ELEMENT_VALUE_LENGTH = 256
MAX_COMPUTER_ELEMENTS = 12


def _reject_unicode_controls(value: object) -> object:
    if isinstance(value, str) and any(unicodedata.category(char).startswith("C") for char in value):
        raise ValueError("text contains a Unicode control character")
    return value


ComputerTypeText = Annotated[
    str,
    Field(min_length=1, max_length=MAX_COMPUTER_TYPE_TEXT_LENGTH,
          pattern=r"^[^\x00-\x1f\x7f]+$"),
    BeforeValidator(_reject_unicode_controls),
]

RequestId = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
]
DeviceId = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
]
CommandId = Literal["pwd", "whoami", "python_version", "git_status", "git_branch"]
ToolName = Literal[
    "system.ping", "terminal.exec", "browser.list_tabs", "browser.navigate",
    "browser.snapshot", "browser.fill", "browser.click", "browser.scroll",
    "browser.type", "browser.back", "computer.capture", "computer.click",
    "computer.type",
]
TOOL_ORDER: tuple[ToolName, ...] = (
    "system.ping", "terminal.exec", "browser.list_tabs", "browser.navigate",
    "browser.snapshot", "browser.fill", "browser.click", "browser.scroll",
    "browser.type", "browser.back", "computer.capture", "computer.click",
    "computer.type",
)


class Message(BaseModel):
    """Base class which rejects unknown wire fields."""

    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1]
    type: str


class Register(Message):
    type: Literal["register"]
    device_id: DeviceId


class Capabilities(Message):
    type: Literal["capabilities"]
    tools: list[
        Literal[
            "system.ping",
            "terminal.exec",
            "browser.list_tabs",
            "browser.navigate",
            "browser.snapshot",
            "browser.fill",
            "browser.click",
            "browser.scroll",
            "browser.type",
            "browser.back",
            "computer.capture",
            "computer.click",
            "computer.type",
        ]
    ] = Field(min_length=1, max_length=MAX_CAPABILITIES)


class Heartbeat(Message):
    type: Literal["heartbeat"]


class AgentResult(Message):
    type: Literal["result"]
    request_id: RequestId
    result: dict[str, object]

    @field_validator("result", mode="before")
    @classmethod
    def bounded_json_result(cls, value: object) -> object:
        """Accept only a reasonably-sized, shallow JSON object result."""
        if not isinstance(value, dict):
            raise ValueError("result must be a JSON object")
        nodes = 0
        stack: list[tuple[object, int]] = [(value, 1)]
        while stack:
            node, depth = stack.pop()
            nodes += 1
            if nodes > MAX_RESULT_NODES:
                raise ValueError("result has too many nodes")
            if depth > MAX_RESULT_DEPTH:
                raise ValueError("result is too deeply nested")
            if isinstance(node, dict):
                for key, child in node.items():
                    if not isinstance(key, str):
                        raise ValueError("result object keys must be strings")
                    stack.append((child, depth + 1))
            elif isinstance(node, list):
                stack.extend((child, depth + 1) for child in node)
            elif not isinstance(node, str | int | float | bool | type(None)):
                raise ValueError("result must contain JSON values")
        try:
            encoded = json.dumps(
                value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("result must contain JSON values") from exc
        if len(encoded) > MAX_RESULT_JSON_BYTES:
            raise ValueError("result JSON exceeds maximum size")
        return value


class ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    code: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_.-]+$")]
    message: Annotated[str, Field(min_length=1, max_length=MAX_ERROR_MESSAGE_LENGTH)]


class AgentError(Message):
    type: Literal["error"]
    request_id: RequestId
    error: ErrorDetail


class Progress(Message):
    type: Literal["progress"]
    request_id: RequestId
    progress: Annotated[int, Field(ge=0, le=100)]
    message: Annotated[str, Field(max_length=MAX_PROGRESS_MESSAGE_LENGTH)] = ""


class Registered(Message):
    type: Literal["registered"]
    device_id: DeviceId


class SystemPingInvoke(Message):
    type: Literal["invoke"]
    request_id: RequestId
    tool: Literal["system.ping"]


class TerminalExecInvoke(Message):
    type: Literal["invoke"]
    request_id: RequestId
    tool: Literal["terminal.exec"]
    command_id: CommandId


class BrowserListTabsInvoke(Message):
    type: Literal["invoke"]
    request_id: RequestId
    tool: Literal["browser.list_tabs"]


class BrowserNavigateInvoke(Message):
    type: Literal["invoke"]
    request_id: RequestId
    tool: Literal["browser.navigate"]
    url: Annotated[str, Field(min_length=1, max_length=MAX_BROWSER_URL_LENGTH)]


class BrowserSnapshotInvoke(Message):
    type: Literal["invoke"]
    request_id: RequestId
    tool: Literal["browser.snapshot"]


class BrowserFillInvoke(Message):
    type: Literal["invoke"]
    request_id: RequestId
    tool: Literal["browser.fill"]
    element_id: Annotated[
        str, Field(min_length=1, max_length=MAX_BROWSER_ELEMENT_ID_LENGTH)
    ]
    value: Annotated[str, Field(min_length=1, max_length=MAX_BROWSER_FILL_VALUE_LENGTH)]


class BrowserClickInvoke(Message):
    type: Literal["invoke"]
    request_id: RequestId
    tool: Literal["browser.click"]
    element_id: Annotated[
        str, Field(min_length=1, max_length=MAX_BROWSER_ELEMENT_ID_LENGTH)
    ]


BrowserScrollDirection = Literal["up", "down"]


class BrowserScrollInvoke(Message):
    type: Literal["invoke"]
    request_id: RequestId
    tool: Literal["browser.scroll"]
    direction: BrowserScrollDirection


class BrowserTypeInvoke(Message):
    type: Literal["invoke"]
    request_id: RequestId
    tool: Literal["browser.type"]
    element_id: Annotated[
        str, Field(min_length=1, max_length=MAX_BROWSER_ELEMENT_ID_LENGTH)
    ]
    text: Annotated[str, Field(min_length=1, max_length=MAX_BROWSER_TYPE_TEXT_LENGTH)]


class BrowserBackInvoke(Message):
    type: Literal["invoke"]
    request_id: RequestId
    tool: Literal["browser.back"]


class ComputerCaptureInvoke(Message):
    type: Literal["invoke"]
    request_id: RequestId
    tool: Literal["computer.capture"]


class ComputerClickInvoke(Message):
    type: Literal["invoke"]
    request_id: RequestId
    tool: Literal["computer.click"]
    element_id: Annotated[
        str, Field(min_length=1, max_length=MAX_COMPUTER_ELEMENT_ID_LENGTH)
    ]


class ComputerTypeInvoke(Message):
    type: Literal["invoke"]
    request_id: RequestId
    tool: Literal["computer.type"]
    element_id: Annotated[
        str, Field(min_length=1, max_length=MAX_COMPUTER_ELEMENT_ID_LENGTH)
    ]
    text: ComputerTypeText


InvokeMessage = (
    SystemPingInvoke
    | TerminalExecInvoke
    | BrowserListTabsInvoke
    | BrowserNavigateInvoke
    | BrowserSnapshotInvoke
    | BrowserFillInvoke
    | BrowserClickInvoke
    | BrowserScrollInvoke
    | BrowserTypeInvoke
    | BrowserBackInvoke
    | ComputerCaptureInvoke
    | ComputerClickInvoke
    | ComputerTypeInvoke
)


class Cancel(Message):
    type: Literal["cancel"]
    request_id: RequestId
    reason: Annotated[str, Field(min_length=1, max_length=256)]


AgentMessage = Register | Capabilities | Heartbeat | AgentResult | AgentError | Progress
ServerMessage = Registered | InvokeMessage | Cancel

_agent_adapter = TypeAdapter(Annotated[AgentMessage, Field(discriminator="type")])


def parse_agent_message(value: object) -> AgentMessage:
    """Parse one decoded JSON object from an agent."""
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
        invoke_types = {
            "system.ping": SystemPingInvoke,
            "terminal.exec": TerminalExecInvoke,
            "browser.list_tabs": BrowserListTabsInvoke,
            "browser.navigate": BrowserNavigateInvoke,
            "browser.snapshot": BrowserSnapshotInvoke,
            "browser.fill": BrowserFillInvoke,
            "browser.click": BrowserClickInvoke,
            "browser.scroll": BrowserScrollInvoke,
            "browser.type": BrowserTypeInvoke,
            "browser.back": BrowserBackInvoke,
            "computer.capture": ComputerCaptureInvoke,
            "computer.click": ComputerClickInvoke,
            "computer.type": ComputerTypeInvoke,
        }
        invoke_type = invoke_types.get(value.get("tool"))
        if invoke_type is not None:
            return invoke_type.model_validate(value)
    raise ValueError("unknown server message")
