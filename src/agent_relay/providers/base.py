"""Minimal, bounded provider tool client boundary."""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Protocol

from pydantic import ValidationError

from ..json_bounds import JsonBoundsError, JsonValue, validate_json_bounds
from ..output_models import ProviderToolResult
from ..provider_tools import (
    MAX_PROVIDER_TOOLS,
    ProviderToolCatalog,
    ProviderToolDescriptor,
)

DEFAULT_PROVIDER_TIMEOUT_SECONDS = 30.0
DEFAULT_PROVIDER_CLOSE_TIMEOUT_SECONDS = 3.0


class ProviderToolError(RuntimeError):
    """A provider operation failed without exposing provider details."""


class ProviderConnectionError(ProviderToolError):
    """The locally configured provider transport was unavailable."""


class ProviderTimeoutError(ProviderToolError):
    """A provider operation exceeded its configured deadline."""


class _ProviderDeadlineExceeded(ProviderTimeoutError):
    """Internal marker emitted only by the adapter's bounded runner."""


class ProviderCleanupError(ProviderToolError):
    """Provider cleanup failed without exposing provider details."""


class UnknownProviderToolError(ProviderToolError):
    """A call named a tool outside the provider's bounded inventory."""


class ProviderToolClient(Protocol):
    async def list_tools(self) -> Sequence[ProviderToolDescriptor]: ...

    async def call_tool(
        self, tool_name: str, arguments: Mapping[str, JsonValue]
    ) -> ProviderToolResult: ...

    async def close(self) -> None: ...


class ProviderAvailability(Protocol):
    """Optional lifecycle signal for clients that can become unavailable."""

    async def wait_unavailable(self) -> None: ...


def validate_timeout(value: float, *, label: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{label} must be greater than zero")
    return float(value)


def bounded_descriptors(
    values: Sequence[ProviderToolDescriptor | Mapping[str, object]],
    *,
    aggregate: bool = True,
) -> tuple[ProviderToolDescriptor, ...]:
    if not aggregate:
        try:
            descriptors = tuple(
                ProviderToolDescriptor.model_validate(value) for value in values
            )
            if len(descriptors) > MAX_PROVIDER_TOOLS:
                raise ValueError
            internal_names = {
                (descriptor.provider_name, descriptor.tool_name)
                for descriptor in descriptors
            }
            public_names = {descriptor.public_name for descriptor in descriptors}
            if len(internal_names) != len(descriptors) or len(public_names) != len(
                descriptors
            ):
                raise ValueError
        except (ValidationError, JsonBoundsError, TypeError, ValueError):
            raise ProviderToolError("invalid provider tool inventory") from None
        return descriptors
    try:
        catalog = ProviderToolCatalog.model_validate({"tools": list(values)})
    except (ValidationError, JsonBoundsError, TypeError, ValueError):
        invalid = True
    else:
        return tuple(catalog.tools)
    if invalid:
        raise ProviderToolError("invalid provider tool inventory")


def bounded_arguments(arguments: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    try:
        validate_json_bounds(arguments, require_object=True, label="provider arguments")
    except (JsonBoundsError, TypeError, ValueError):
        invalid = True
    else:
        return arguments
    if invalid:
        raise ProviderToolError("invalid provider arguments")


def validate_provider_arguments(
    descriptor: ProviderToolDescriptor,
    arguments: Mapping[str, JsonValue],
) -> Mapping[str, JsonValue]:
    """Validate one bounded argument object against a provider schema.

    This intentionally implements only the bounded JSON-Schema subset accepted
    by ``validate_json_schema``.  Unknown assertions fail closed rather than
    allowing an invocation to bypass a provider's declared contract.
    """
    bounded = bounded_arguments(arguments)
    try:
        _validate_schema_node(descriptor.input_schema, bounded, path="$")
    except (TypeError, ValueError, KeyError, re.error):
        raise ProviderToolError("provider arguments do not match tool schema") from None
    return bounded


def _validate_schema_node(schema: object, value: object, *, path: str) -> None:
    if not isinstance(schema, Mapping):
        raise ValueError("schema node must be an object")
    if any(
        key in schema
        for key in (
            "$ref",
            "$defs",
            "definitions",
            "if",
            "then",
            "else",
            "contains",
            "uniqueItems",
            "prefixItems",
            "dependentSchemas",
            "dependentRequired",
            "propertyNames",
            "unevaluatedProperties",
            "unevaluatedItems",
            "contentSchema",
            "format",
            "multipleOf",
        )
    ):
        raise ValueError("schema assertion is not supported at this boundary")

    if "const" in schema and value != schema["const"]:
        raise ValueError("const assertion failed")
    if "enum" in schema:
        choices = schema["enum"]
        if not isinstance(choices, list) or value not in choices:
            raise ValueError("enum assertion failed")

    for key in ("allOf", "anyOf", "oneOf"):
        if key not in schema:
            continue
        branches = schema[key]
        if not isinstance(branches, list) or not branches:
            raise ValueError("invalid schema combinator")
        outcomes: list[bool] = []
        for branch in branches:
            try:
                _validate_schema_node(branch, value, path=path)
            except (TypeError, ValueError, KeyError, re.error):
                outcomes.append(False)
            else:
                outcomes.append(True)
        if key == "allOf" and not all(outcomes):
            raise ValueError("allOf assertion failed")
        if key == "anyOf" and not any(outcomes):
            raise ValueError("anyOf assertion failed")
        if key == "oneOf" and sum(outcomes) != 1:
            raise ValueError("oneOf assertion failed")

    if "not" in schema:
        try:
            _validate_schema_node(schema["not"], value, path=path)
        except (TypeError, ValueError, KeyError, re.error):
            pass
        else:
            raise ValueError("not assertion failed")

    declared_type = schema.get("type")
    if declared_type is not None:
        types = declared_type if isinstance(declared_type, list) else [declared_type]
        if not types or not all(isinstance(item, str) for item in types):
            raise ValueError("invalid schema type")
        if not any(_matches_json_type(item, value) for item in types):
            raise ValueError("type assertion failed")

    if "properties" in schema or "required" in schema or "additionalProperties" in schema:
        if not isinstance(value, dict):
            raise ValueError("object assertion failed")
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping):
            raise ValueError("properties must be an object")
        required = schema.get("required", [])
        if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
            raise ValueError("required must be a string list")
        for name in required:
            if name not in value:
                raise ValueError("required property is missing")
        if "minProperties" in schema and len(value) < _integer_limit(schema["minProperties"]):
            raise ValueError("minProperties assertion failed")
        if "maxProperties" in schema and len(value) > _integer_limit(schema["maxProperties"]):
            raise ValueError("maxProperties assertion failed")
        additional = schema.get("additionalProperties", False)
        if additional is not False and additional is not True and not isinstance(additional, Mapping):
            raise ValueError("invalid additionalProperties")
        for name, child in value.items():
            if name in properties:
                _validate_schema_node(properties[name], child, path=f"{path}.{name}")
            elif additional is False:
                raise ValueError("additional property is not allowed")
            elif isinstance(additional, Mapping):
                _validate_schema_node(additional, child, path=f"{path}.{name}")

    if "items" in schema:
        if not isinstance(value, list):
            raise ValueError("array assertion failed")
        items = schema["items"]
        if not isinstance(items, Mapping):
            raise ValueError("items must be a schema object")
        if "minItems" in schema and len(value) < _integer_limit(schema["minItems"]):
            raise ValueError("minItems assertion failed")
        if "maxItems" in schema and len(value) > _integer_limit(schema["maxItems"]):
            raise ValueError("maxItems assertion failed")
        for index, child in enumerate(value):
            _validate_schema_node(items, child, path=f"{path}[{index}]")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < _integer_limit(schema["minLength"]):
            raise ValueError("minLength assertion failed")
        if "maxLength" in schema and len(value) > _integer_limit(schema["maxLength"]):
            raise ValueError("maxLength assertion failed")
        if "pattern" in schema:
            pattern = schema["pattern"]
            if not isinstance(pattern, str) or re.search(pattern, value) is None:
                raise ValueError("pattern assertion failed")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < _number_limit(schema["minimum"]):
            raise ValueError("minimum assertion failed")
        if "maximum" in schema and value > _number_limit(schema["maximum"]):
            raise ValueError("maximum assertion failed")
        if "exclusiveMinimum" in schema and value <= _number_limit(schema["exclusiveMinimum"]):
            raise ValueError("exclusiveMinimum assertion failed")
        if "exclusiveMaximum" in schema and value >= _number_limit(schema["exclusiveMaximum"]):
            raise ValueError("exclusiveMaximum assertion failed")


def _matches_json_type(type_name: str, value: object) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(type_name, False)


def _integer_limit(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("schema limit must be a non-negative integer")
    return value


def _number_limit(value: object) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("schema limit must be a finite number")
    return value


def bounded_result(value: object) -> ProviderToolResult:
    try:
        return ProviderToolResult.model_validate(_model_data(value))
    except (ValidationError, JsonBoundsError, TypeError, ValueError):
        invalid = True
    if invalid:
        raise ProviderToolError("invalid provider result")


async def run_bounded(
    operation: Callable[[], Awaitable[object]],
    timeout_seconds: float,
    pending_tasks: set[asyncio.Task[object]],
) -> object:
    task = asyncio.create_task(operation())
    pending_tasks.add(task)
    task.add_done_callback(
        lambda completed: _release_task(completed, pending_tasks)
    )
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout_seconds)
    except asyncio.CancelledError:
        task.cancel()
        raise
    if task in done:
        return task.result()

    # A provider may suppress cancellation. Detach it so the public operation
    # still returns at its deadline. The owning client keeps it registered until
    # it actually finishes because Python cannot force-kill an in-process task.
    task.cancel()
    raise _ProviderDeadlineExceeded("provider operation timed out")


async def drain_pending_tasks(
    pending_tasks: set[asyncio.Task[object]], timeout_seconds: float
) -> bool:
    tasks = set(pending_tasks)
    if not tasks:
        return True
    for task in tasks:
        task.cancel()
    try:
        done, pending = await asyncio.wait(tasks, timeout=max(0.0, timeout_seconds))
    except asyncio.CancelledError:
        for task in pending_tasks:
            task.cancel()
        raise
    for task in done:
        _release_task(task, pending_tasks)
    return not pending


def _release_task(
    task: asyncio.Task[object], pending_tasks: set[asyncio.Task[object]]
) -> None:
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        pass
    pending_tasks.discard(task)


def _model_data(value: object) -> object:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json", by_alias=True, exclude_none=True)
    return value


__all__ = [
    "DEFAULT_PROVIDER_CLOSE_TIMEOUT_SECONDS",
    "DEFAULT_PROVIDER_TIMEOUT_SECONDS",
    "ProviderConnectionError",
    "ProviderCleanupError",
    "ProviderAvailability",
    "ProviderToolClient",
    "ProviderToolError",
    "ProviderTimeoutError",
    "UnknownProviderToolError",
    "validate_provider_arguments",
]
