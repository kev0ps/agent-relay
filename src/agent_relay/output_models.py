"""Shared closed result models for every public Relay boundary."""

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
    MAX_JSON_BYTES,
    MAX_JSON_COLLECTION_ITEMS,
    MAX_JSON_URI_LENGTH,
    JsonObject,
    validate_json_bounds,
    validate_resource_uri,
)

# Temporary semantic-v1 DTO limits. Keeping these local avoids coupling the
# provider-neutral result model back to the WebSocket protocol module. Task 5
# removes the legacy semantic output inventory below.
MAX_BROWSER_URL_LENGTH = 2048
MAX_BROWSER_ELEMENT_ID_LENGTH = 128
MAX_BROWSER_TAB_ID_LENGTH = 128
MAX_BROWSER_TITLE_LENGTH = 256
MAX_BROWSER_PAGE_TEXT_LENGTH = 4096
MAX_BROWSER_ROLE_LENGTH = 64
MAX_BROWSER_NAME_LENGTH = 128
MAX_BROWSER_ELEMENT_VALUE_LENGTH = 256
MAX_BROWSER_TABS = 6
MAX_BROWSER_ELEMENTS = 12
MAX_COMPUTER_ELEMENT_ID_LENGTH = 128
MAX_COMPUTER_APP_LENGTH = 128
MAX_COMPUTER_WINDOW_TITLE_LENGTH = 256
MAX_COMPUTER_GENERATION_LENGTH = 128
MAX_COMPUTER_ROLE_LENGTH = 64
MAX_COMPUTER_NAME_LENGTH = 128
MAX_COMPUTER_ELEMENT_VALUE_LENGTH = 256
MAX_COMPUTER_ELEMENTS = 12
CommandId = Literal["pwd", "whoami", "python_version", "git_status", "git_branch"]
ToolName = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
]


class Output(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        populate_by_name=True,
        hide_input_in_errors=True,
    )


class PingOutput(Output):
    pong: bool


class TerminalExecOutput(Output):
    command_id: CommandId
    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool
    stdout_truncated: bool
    stderr_truncated: bool


BrowserUrl = Annotated[str, Field(min_length=1, max_length=MAX_BROWSER_URL_LENGTH)]
BrowserTabId = Annotated[str, Field(min_length=1, max_length=MAX_BROWSER_TAB_ID_LENGTH)]
BrowserElementId = Annotated[str, Field(min_length=1, max_length=MAX_BROWSER_ELEMENT_ID_LENGTH)]


class BrowserElementOutput(Output):
    element_id: BrowserElementId
    role: Annotated[str, Field(min_length=1, max_length=MAX_BROWSER_ROLE_LENGTH)]
    name: Annotated[str, Field(max_length=MAX_BROWSER_NAME_LENGTH)]
    value: Annotated[str, Field(max_length=MAX_BROWSER_ELEMENT_VALUE_LENGTH)] | None
    editable: bool
    enabled: bool


class BrowserTabOutput(Output):
    tab_id: BrowserTabId
    title: Annotated[str, Field(max_length=MAX_BROWSER_TITLE_LENGTH)]
    url: BrowserUrl


class BrowserTabsOutput(Output):
    tabs: list[BrowserTabOutput] = Field(max_length=MAX_BROWSER_TABS)


class BrowserPageOutput(Output):
    tab_id: BrowserTabId
    title: Annotated[str, Field(max_length=MAX_BROWSER_TITLE_LENGTH)]
    url: BrowserUrl
    text: Annotated[str, Field(max_length=MAX_BROWSER_PAGE_TEXT_LENGTH)]
    elements: list[BrowserElementOutput] = Field(max_length=MAX_BROWSER_ELEMENTS)


class BrowserActionOutput(Output):
    tab_id: BrowserTabId
    element_id: BrowserElementId | None
    url: BrowserUrl
    title: Annotated[str, Field(max_length=MAX_BROWSER_TITLE_LENGTH)]
    success: bool


ComputerElementId = Annotated[str, Field(min_length=1, max_length=MAX_COMPUTER_ELEMENT_ID_LENGTH)]
ComputerGeneration = Annotated[str, Field(min_length=1, max_length=MAX_COMPUTER_GENERATION_LENGTH)]


class ComputerElementOutput(Output):
    element_id: ComputerElementId
    role: Annotated[str, Field(min_length=1, max_length=MAX_COMPUTER_ROLE_LENGTH)]
    name: Annotated[str, Field(max_length=MAX_COMPUTER_NAME_LENGTH)]
    value: Annotated[str, Field(max_length=MAX_COMPUTER_ELEMENT_VALUE_LENGTH)] | None
    enabled: bool


class ComputerCaptureOutput(Output):
    app: Annotated[str, Field(min_length=1, max_length=MAX_COMPUTER_APP_LENGTH)]
    window_title: Annotated[str, Field(max_length=MAX_COMPUTER_WINDOW_TITLE_LENGTH)]
    generation: ComputerGeneration
    elements: list[ComputerElementOutput] = Field(max_length=MAX_COMPUTER_ELEMENTS)


class ComputerActionOutput(Output):
    success: bool
    generation: ComputerGeneration
    element_id: ComputerElementId


class _ProviderContent(Output):
    """Shared bounded MCP metadata for provider content blocks."""

    annotations: JsonObject | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    meta: JsonObject | None = Field(
        default=None,
        validation_alias=AliasChoices("_meta", "meta"),
        serialization_alias="_meta",
        exclude_if=lambda value: value is None,
    )

    @field_validator("annotations", "meta", mode="before")
    @classmethod
    def _bounded_metadata(cls, value: object) -> object:
        if value is None:
            return None
        return validate_json_bounds(
            value,
            require_object=True,
            label="content metadata",
        )

    @model_validator(mode="after")
    def _bounded_content(self) -> "_ProviderContent":
        validate_json_bounds(
            self.model_dump(mode="json", by_alias=True, exclude_none=True),
            require_object=True,
            label="content",
        )
        return self


class ProviderTextContent(_ProviderContent):
    """MCP text content preserved without semantic interpretation."""

    type: Literal["text"]
    text: str = Field(max_length=MAX_JSON_BYTES)


class ProviderImageContent(_ProviderContent):
    """MCP image content with bounded, opaque encoded data."""

    type: Literal["image"]
    data: str = Field(min_length=1, max_length=MAX_JSON_BYTES)
    mime_type: str = Field(
        min_length=1,
        max_length=128,
        validation_alias=AliasChoices("mimeType", "mime_type"),
        serialization_alias="mimeType",
    )


class ProviderAudioContent(_ProviderContent):
    """MCP audio content with bounded, opaque encoded data."""

    type: Literal["audio"]
    data: str = Field(min_length=1, max_length=MAX_JSON_BYTES)
    mime_type: str = Field(
        min_length=1,
        max_length=128,
        validation_alias=AliasChoices("mimeType", "mime_type"),
        serialization_alias="mimeType",
    )


ProviderResourceUri = Annotated[
    str,
    Field(min_length=1, max_length=MAX_JSON_URI_LENGTH),
]


class ProviderEmbeddedResource(Output):
    """A resource body with a conservative network URI policy."""

    model_config = ConfigDict(
        json_schema_extra={
            "oneOf": [
                {
                    "required": ["text"],
                    "properties": {"text": {"type": "string"}},
                    "not": {"required": ["blob"]},
                },
                {
                    "required": ["blob"],
                    "properties": {"blob": {"type": "string"}},
                    "not": {"required": ["text"]},
                },
            ]
        }
    )

    uri: ProviderResourceUri
    mime_type: str | None = Field(
        default=None,
        max_length=128,
        validation_alias=AliasChoices("mimeType", "mime_type"),
        serialization_alias="mimeType",
    )
    text: str | None = Field(
        default=None,
        max_length=MAX_JSON_BYTES,
        exclude_if=lambda value: value is None,
    )
    blob: str | None = Field(
        default=None,
        max_length=MAX_JSON_BYTES,
        exclude_if=lambda value: value is None,
    )
    meta: JsonObject | None = Field(
        default=None,
        validation_alias=AliasChoices("_meta", "meta"),
        serialization_alias="_meta",
        exclude_if=lambda value: value is None,
    )

    @field_validator("uri", mode="before")
    @classmethod
    def _safe_uri(cls, value: object) -> object:
        return validate_resource_uri(value)

    @field_validator("text", "blob", mode="before")
    @classmethod
    def _reject_explicit_null_body(cls, value: object) -> object:
        if value is None:
            raise ValueError("resource body must be a non-null string")
        return value

    @field_validator("meta", mode="before")
    @classmethod
    def _bounded_metadata(cls, value: object) -> object:
        if value is None:
            return None
        return validate_json_bounds(
            value,
            require_object=True,
            label="resource metadata",
        )

    @model_validator(mode="after")
    def _one_resource_body(self) -> "ProviderEmbeddedResource":
        if (self.text is None) == (self.blob is None):
            raise ValueError("resource must contain exactly one text or blob body")
        return self

    @model_validator(mode="after")
    def _bounded_resource(self) -> "ProviderEmbeddedResource":
        validate_json_bounds(
            self.model_dump(mode="json", by_alias=True, exclude_none=True),
            require_object=True,
            label="resource",
        )
        return self


class ProviderResourceContent(_ProviderContent):
    """MCP embedded resource content, retained as provider-supplied data."""

    type: Literal["resource"]
    resource: ProviderEmbeddedResource


class ProviderResourceLinkContent(_ProviderContent):
    """MCP resource-link content with no local-file or credential URI schemes."""

    type: Literal["resource_link"]
    uri: ProviderResourceUri
    name: str = Field(
        min_length=1,
        max_length=MAX_BROWSER_TITLE_LENGTH,
    )
    title: str | None = Field(default=None, max_length=MAX_BROWSER_TITLE_LENGTH)
    description: str | None = Field(default=None, max_length=MAX_BROWSER_PAGE_TEXT_LENGTH)
    mime_type: str | None = Field(
        default=None,
        max_length=128,
        validation_alias=AliasChoices("mimeType", "mime_type"),
        serialization_alias="mimeType",
    )
    size: int | None = Field(default=None, ge=0, le=MAX_JSON_BYTES)
    icons: list[JsonObject] | None = Field(
        default=None,
        max_length=MAX_JSON_COLLECTION_ITEMS,
    )

    @field_validator("uri", mode="before")
    @classmethod
    def _safe_uri(cls, value: object) -> object:
        return validate_resource_uri(value)

    @field_validator("icons", mode="before")
    @classmethod
    def _bounded_icons(cls, value: object) -> object:
        if value is None:
            return None
        return validate_json_bounds(value, label="resource link icons")


ProviderContent = (
    ProviderTextContent
    | ProviderImageContent
    | ProviderAudioContent
    | ProviderResourceContent
    | ProviderResourceLinkContent
)


class ProviderToolResult(Output):
    """MCP-compatible bounded result; content is not mapped to semantic DTOs."""

    content: list[ProviderContent] = Field(
        max_length=MAX_JSON_COLLECTION_ITEMS,
    )
    structured_content: JsonObject | None = Field(
        default=None,
        validation_alias=AliasChoices("structuredContent", "structured_content"),
        serialization_alias="structuredContent",
    )
    is_error: bool = Field(
        default=False,
        validation_alias=AliasChoices("isError", "is_error"),
        serialization_alias="isError",
    )
    meta: JsonObject | None = Field(
        default=None,
        validation_alias=AliasChoices("_meta", "meta"),
        serialization_alias="_meta",
        exclude_if=lambda value: value is None,
    )

    @field_validator("structured_content", mode="before")
    @classmethod
    def _bounded_structured_content(cls, value: object) -> object:
        if value is None:
            return None
        return validate_json_bounds(
            value,
            require_object=True,
            label="structuredContent",
        )

    @field_validator("meta", mode="before")
    @classmethod
    def _bounded_metadata(cls, value: object) -> object:
        if value is None:
            return None
        return validate_json_bounds(
            value,
            require_object=True,
            label="result metadata",
        )

    @model_validator(mode="after")
    def _bounded_result(self) -> "ProviderToolResult":
        validate_json_bounds(
            self.model_dump(mode="json", by_alias=True, exclude_none=True),
            require_object=True,
            label="provider result",
        )
        return self


# Keep the original semantic DTO inventory below intact for the current v1 path.
OUTPUT_BY_TOOL: dict[ToolName, type[Output]] = {
    "system.ping": PingOutput, "terminal.exec": TerminalExecOutput,
    "browser.list_tabs": BrowserTabsOutput, "browser.navigate": BrowserActionOutput,
    "browser.snapshot": BrowserPageOutput, "browser.fill": BrowserActionOutput,
    "browser.click": BrowserActionOutput, "browser.scroll": BrowserActionOutput,
    "browser.type": BrowserActionOutput, "browser.back": BrowserActionOutput,
    "computer.capture": ComputerCaptureOutput,
    "computer.click": ComputerActionOutput, "computer.type": ComputerActionOutput,
}


def validate_tool_output(tool: ToolName, result: object) -> Output:
    return OUTPUT_BY_TOOL[tool].model_validate(result)
