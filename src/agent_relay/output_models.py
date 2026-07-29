"""Shared closed result models for every public Relay boundary."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from .protocol import (
    MAX_BROWSER_ELEMENT_ID_LENGTH,
    MAX_BROWSER_ELEMENT_VALUE_LENGTH,
    MAX_BROWSER_ELEMENTS,
    MAX_BROWSER_NAME_LENGTH,
    MAX_BROWSER_PAGE_TEXT_LENGTH,
    MAX_BROWSER_ROLE_LENGTH,
    MAX_BROWSER_TAB_ID_LENGTH,
    MAX_BROWSER_TABS,
    MAX_BROWSER_TITLE_LENGTH,
    MAX_BROWSER_URL_LENGTH,
    MAX_COMPUTER_APP_LENGTH,
    MAX_COMPUTER_ELEMENT_ID_LENGTH,
    MAX_COMPUTER_ELEMENT_VALUE_LENGTH,
    MAX_COMPUTER_ELEMENTS,
    MAX_COMPUTER_GENERATION_LENGTH,
    MAX_COMPUTER_NAME_LENGTH,
    MAX_COMPUTER_ROLE_LENGTH,
    MAX_COMPUTER_WINDOW_TITLE_LENGTH,
    CommandId,
    ToolName,
)


class Output(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


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


OUTPUT_BY_TOOL: dict[ToolName, type[Output]] = {
    "system.ping": PingOutput, "terminal.exec": TerminalExecOutput,
    "browser.list_tabs": BrowserTabsOutput, "browser.navigate": BrowserActionOutput,
    "browser.read_page": BrowserPageOutput, "browser.fill": BrowserActionOutput,
    "browser.click": BrowserActionOutput, "computer.capture": ComputerCaptureOutput,
    "computer.click": ComputerActionOutput, "computer.type": ComputerActionOutput,
}


def validate_tool_output(tool: ToolName, result: object) -> Output:
    return OUTPUT_BY_TOOL[tool].model_validate(result)
