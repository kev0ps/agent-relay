"""Shared strict bounds for JSON-shaped provider metadata and payloads."""

from __future__ import annotations

import json
import math
import re
from typing import NoReturn, TypeAlias
from urllib.parse import parse_qsl, urlsplit

from typing_extensions import TypeAliasType

MAX_JSON_BYTES = 64 * 1024
MAX_JSON_DEPTH = 16
MAX_JSON_NODES = 4096
MAX_JSON_COLLECTION_ITEMS = 256
MAX_JSON_URI_LENGTH = 2048

JsonPrimitive: TypeAlias = str | int | float | bool | None
JsonValue = TypeAliasType(
    "JsonValue",
    JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"],
)
JsonObject = TypeAliasType("JsonObject", dict[str, JsonValue])


class JsonBoundsError(ValueError):
    """Raised when a value is not safe for a bounded JSON boundary."""


_UNSAFE_METADATA_WORDS = {
    "handler",
    "module",
    "executable",
    "exec",
    "code",
    "script",
    "callback",
    "callable",
    "command",
    "function",
    "entrypoint",
    "endpoint",
    "execute",
    "shell",
}
_SENSITIVE_QUERY_KEY = {
    "accesstoken",
    "apikey",
    "auth",
    "authtoken",
    "authorization",
    "bearer",
    "clientsecret",
    "credential",
    "jwt",
    "oauthtoken",
    "password",
    "privatekey",
    "refreshtoken",
    "secret",
    "sig",
    "signature",
    "token",
}


def validate_resource_uri(value: object) -> str:
    """Validate a conservative, bounded URI suitable for provider resources."""
    if not isinstance(value, str):
        raise JsonBoundsError("resource URI must be a string")
    if not value or len(value) > MAX_JSON_URI_LENGTH:
        raise JsonBoundsError("resource URI is empty or too long")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise JsonBoundsError("resource URI contains a control character")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise JsonBoundsError("resource URI is malformed") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise JsonBoundsError("resource URI scheme is not allowed")
    if parsed.username is not None or parsed.password is not None:
        raise JsonBoundsError("resource URI must not contain credentials")
    try:
        if parsed.port is not None and not 0 < parsed.port <= 65535:
            raise JsonBoundsError("resource URI port is invalid")
    except ValueError as exc:
        raise JsonBoundsError("resource URI port is invalid") from exc
    try:
        hostname = parsed.hostname
    except ValueError as exc:
        raise JsonBoundsError("resource URI host is malformed") from exc
    if not hostname:
        raise JsonBoundsError("resource URI host is missing")
    if any(
        _normalize_query_key(key) in _SENSITIVE_QUERY_KEY
        for key, _value in parse_qsl(parsed.query, keep_blank_values=True)
    ):
        raise JsonBoundsError("resource URI contains credential-like query data")
    return value


def _normalize_query_key(key: str) -> str:
    """Normalize query-key spelling before applying the credential policy."""
    return re.sub(r"[^a-z0-9]", "", key.casefold())


def _normalize_metadata_key(key: str) -> str:
    """Normalize separators and camel-case for executable-key checks."""
    camel_case = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
    return re.sub(r"[^a-z0-9]+", "_", camel_case.casefold()).strip("_")


def _reject_unsafe_metadata_key(key: str) -> None:
    normalized = _normalize_metadata_key(key)
    if key == "command_id":
        return
    if any(part in _UNSAFE_METADATA_WORDS for part in normalized.split("_")):
        raise JsonBoundsError(f"JSON metadata key is not allowed: {key!r}")


def validate_json_bounds(
    value: object,
    *,
    require_object: bool = False,
    label: str = "value",
    reject_unsafe_metadata: bool = False,
) -> object:
    """Validate and return a finite, bounded JSON value without transforming it.

    The traversal is shared by descriptors, provider invocations, structured
    content, and protocol results.  It deliberately accepts only the JSON
    types themselves; tuples, sets, model instances, bytes, and other Python
    objects are rejected instead of being stringified or otherwise adapted.
    """
    if require_object and not isinstance(value, dict):
        raise JsonBoundsError(f"{label} must be a JSON object")

    nodes = 0
    stack: list[tuple[object, int, str | None]] = [(value, 1, None)]
    while stack:
        node, depth, parent_key = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise JsonBoundsError(f"{label} has too many nodes")
        if depth > MAX_JSON_DEPTH:
            raise JsonBoundsError(f"{label} is too deeply nested")

        if isinstance(node, dict):
            if len(node) > MAX_JSON_COLLECTION_ITEMS:
                raise JsonBoundsError(f"{label} has too many object members")
            for key, child in node.items():
                if not isinstance(key, str):
                    raise JsonBoundsError(f"{label} object keys must be strings")
                if reject_unsafe_metadata:
                    _reject_unsafe_metadata_key(key)
                stack.append((child, depth + 1, key))
        elif isinstance(node, list):
            if len(node) > MAX_JSON_COLLECTION_ITEMS:
                raise JsonBoundsError(f"{label} has too many array items")
            stack.extend((child, depth + 1, parent_key) for child in node)
        elif isinstance(node, str):
            continue
        elif isinstance(node, bool) or node is None:
            continue
        elif isinstance(node, int) and not isinstance(node, bool):
            continue
        elif isinstance(node, float) and math.isfinite(node):
            continue
        else:
            raise JsonBoundsError(f"{label} must contain only JSON values")

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise JsonBoundsError(f"{label} must contain only JSON values") from exc
    if len(encoded) > MAX_JSON_BYTES:
        raise JsonBoundsError(f"{label} JSON exceeds maximum size")
    return value


def validate_json_schema(value: object) -> object:
    """Validate a bounded, semantically closed provider JSON Schema.

    Generic JSON bounds prove that a schema is serializable and finite, but do
    not prove that the schema itself is bounded.  This policy requires closed
    object schemas, bounded arrays, and local non-recursive definitions only.
    """
    validate_json_bounds(
        value,
        require_object=True,
        label="schema",
        reject_unsafe_metadata=True,
    )
    if not isinstance(value, dict):  # pragma: no cover - guarded above
        raise JsonBoundsError("schema must be a JSON object")

    def fail(message: str) -> NoReturn:
        raise JsonBoundsError(f"schema {message}")

    def bounded_limit(node: dict[str, object], key: str) -> None:
        if key not in node:
            return
        limit = node[key]
        if type(limit) is not int or not 0 <= limit <= MAX_JSON_COLLECTION_ITEMS:
            fail(f"{key} must be a bounded integer")

    def definition_map(node: dict[str, object], key: str) -> dict[str, object] | None:
        definitions = node.get(key)
        if definitions is None:
            return None
        if not isinstance(definitions, dict):
            fail(f"{key} must be an object")
        for name, definition in definitions.items():
            if not isinstance(name, str) or not isinstance(definition, dict):
                fail(f"{key} must contain schema objects")
        return definitions

    def resolve_local_ref(ref: str) -> tuple[str, dict[str, object]]:
        if ref == "#":
            fail("must not contain a self-referential root $ref")
        for prefix, container_name in (
            ("#/$defs/", "$defs"),
            ("#/definitions/", "definitions"),
        ):
            if ref.startswith(prefix):
                name = ref[len(prefix) :]
                if not name or "/" in name or name in {".", ".."}:
                    fail("contains an unsafe local $ref")
                definitions = definition_map(value, container_name)
                if definitions is None or name not in definitions:
                    fail("references a missing local definition")
                target = definitions[name]
                if not isinstance(target, dict):  # pragma: no cover - prevalidated
                    fail("references a non-object local definition")
                return ref, target
        fail("must reference a bounded local definition")
        raise AssertionError("unreachable")

    schema_keywords = {
        "allOf",
        "anyOf",
        "oneOf",
        "not",
        "if",
        "then",
        "else",
        "contains",
        "propertyNames",
        "unevaluatedItems",
        "contentSchema",
    }
    object_keywords = {
        "properties",
        "additionalProperties",
        "patternProperties",
        "propertyNames",
        "unevaluatedProperties",
        "dependentSchemas",
        "dependentRequired",
        "maxProperties",
        "minProperties",
        "required",
    }
    array_keywords = {
        "items",
        "prefixItems",
        "contains",
        "maxItems",
        "minItems",
        "uniqueItems",
    }

    def walk(node: object, path: str, active_refs: frozenset[str]) -> None:
        if not isinstance(node, dict):
            fail(f"at {path} must be a schema object")
        if not node:
            fail(f"at {path} is unbounded")

        ref = node.get("$ref")
        if ref is not None:
            if not isinstance(ref, str):
                fail(f"at {path} has a non-string $ref")
            resolved_ref, target = resolve_local_ref(ref)
            if resolved_ref in active_refs:
                fail(f"at {path} contains a recursive $ref")
            walk(target, f"{path} ({resolved_ref})", active_refs | {resolved_ref})
        for ref_key in ("$dynamicRef", "$recursiveRef"):
            if ref_key in node:
                fail(f"at {path} contains unsupported {ref_key}")

        defs = definition_map(node, "$defs")
        legacy_defs = definition_map(node, "definitions")
        for definitions, key in ((defs, "$defs"), (legacy_defs, "definitions")):
            if definitions is not None:
                for name, definition in definitions.items():
                    walk(definition, f"{path}.{key}.{name}", active_refs)

        schema_type = node.get("type")
        valid_schema_types = {
            "null",
            "boolean",
            "object",
            "array",
            "number",
            "integer",
            "string",
        }
        if isinstance(schema_type, str):
            types = {schema_type}
        elif isinstance(schema_type, list) and all(isinstance(item, str) for item in schema_type):
            types = set(schema_type)
        elif schema_type is None:
            types = set()
        else:
            fail(f"at {path} has an invalid type")
        if schema_type is not None and (not types or not types <= valid_schema_types):
            fail(f"at {path} has an invalid type")

        for key in (
            "minLength",
            "maxLength",
            "minItems",
            "maxItems",
            "minProperties",
            "maxProperties",
        ):
            bounded_limit(node, key)
        for key in ("pattern", "format", "contentEncoding", "contentMediaType"):
            if key in node and not isinstance(node[key], str):
                fail(f"at {path}.{key} must be a string")
        for key in (
            "minimum",
            "maximum",
            "exclusiveMinimum",
            "exclusiveMaximum",
            "multipleOf",
        ):
            if key not in node:
                continue
            bound = node[key]
            if isinstance(bound, bool) or not isinstance(bound, (int, float)) or not math.isfinite(bound):
                fail(f"at {path}.{key} must be a finite number")
            if key == "multipleOf" and bound <= 0:
                fail(f"at {path}.{key} must be positive")
        for key in ("uniqueItems", "deprecated", "readOnly", "writeOnly"):
            if key in node and not isinstance(node[key], bool):
                fail(f"at {path}.{key} must be a boolean")
        required = node.get("required")
        if required is not None:
            if (
                not isinstance(required, list)
                or len(required) > MAX_JSON_COLLECTION_ITEMS
                or any(not isinstance(name, str) for name in required)
                or len(set(required)) != len(required)
            ):
                fail(f"at {path}.required must be a bounded unique string array")
        dependent_required = node.get("dependentRequired")
        if dependent_required is not None:
            if not isinstance(dependent_required, dict):
                fail(f"at {path}.dependentRequired must be an object")
            for name, names in dependent_required.items():
                if (
                    not isinstance(name, str)
                    or not isinstance(names, list)
                    or len(names) > MAX_JSON_COLLECTION_ITEMS
                    or any(not isinstance(item, str) for item in names)
                    or len(set(names)) != len(names)
                ):
                    fail(f"at {path}.dependentRequired must contain bounded string arrays")

        inferred_object = bool(object_keywords & node.keys())
        inferred_array = bool(array_keywords & node.keys())
        if not types and not inferred_object and not inferred_array:
            allowed_assertions = {
                "$defs",
                "definitions",
                "$ref",
                "const",
                "enum",
                "allOf",
                "anyOf",
                "oneOf",
                "not",
                "if",
                "then",
                "else",
                "title",
                "description",
                "default",
                "examples",
                "deprecated",
                "readOnly",
                "writeOnly",
            }
            if not (set(node) & allowed_assertions - {"title", "description", "default", "examples", "deprecated", "readOnly", "writeOnly"}):
                fail(f"at {path} is unbounded")

        if "object" in types or inferred_object:
            additional = node.get("additionalProperties", None)
            if additional is None:
                fail(f"at {path} leaves additionalProperties open")
            if additional is True:
                fail(f"at {path} has boolean additionalProperties=true")
            if isinstance(additional, dict):
                if "maxProperties" not in node:
                    fail(f"at {path} mapping additionalProperties requires maxProperties")
                bounded_limit(node, "maxProperties")
                walk(additional, f"{path}.additionalProperties", active_refs)
            elif additional is not False:
                fail(f"at {path} has invalid additionalProperties")
            if "patternProperties" in node:
                fail(f"at {path} has open patternProperties")
            properties = node.get("properties", {})
            if not isinstance(properties, dict):
                fail(f"at {path}.properties must be an object")
            for name, property_schema in properties.items():
                if not isinstance(name, str):
                    fail(f"at {path}.properties has a non-string name")
                walk(property_schema, f"{path}.properties.{name}", active_refs)
            for name in ("propertyNames", "unevaluatedProperties"):
                if name in node:
                    candidate = node[name]
                    if candidate is True:
                        fail(f"at {path} has open {name}")
                    if candidate is not False:
                        walk(candidate, f"{path}.{name}", active_refs)
            dependent = node.get("dependentSchemas")
            if dependent is not None:
                if not isinstance(dependent, dict):
                    fail(f"at {path}.dependentSchemas must be an object")
                for name, dependent_schema in dependent.items():
                    if not isinstance(name, str):
                        fail(f"at {path}.dependentSchemas has a non-string name")
                    walk(dependent_schema, f"{path}.dependentSchemas.{name}", active_refs)
            bounded_limit(node, "maxProperties")

        if "array" in types or inferred_array:
            items = node.get("items")
            prefix_items = node.get("prefixItems")
            if prefix_items is not None:
                if not isinstance(prefix_items, list):
                    fail(f"at {path}.prefixItems must be an array")
                for index, item_schema in enumerate(prefix_items):
                    walk(item_schema, f"{path}.prefixItems[{index}]", active_refs)
            if items is None:
                fail(f"at {path} leaves array items open")
            elif items is True:
                fail(f"at {path} has boolean items=true")
            elif items is False:
                pass
            elif isinstance(items, dict):
                bounded_limit(node, "maxItems")
                walk(items, f"{path}.items", active_refs)
            elif isinstance(items, list):
                for index, item_schema in enumerate(items):
                    walk(item_schema, f"{path}.items[{index}]", active_refs)
            else:
                fail(f"at {path} has invalid items")
            if items is not False and "maxItems" not in node:
                fail(f"at {path} leaves maxItems unbounded")
            bounded_limit(node, "maxItems")

        for key in schema_keywords:
            candidate = node.get(key)
            if candidate is None:
                continue
            if key in {"allOf", "anyOf", "oneOf"}:
                if not isinstance(candidate, list):
                    fail(f"at {path}.{key} must be an array")
                for index, branch in enumerate(candidate):
                    walk(branch, f"{path}.{key}[{index}]", active_refs)
            else:
                walk(candidate, f"{path}.{key}", active_refs)

    walk(value, "$", frozenset())
    return value


# Descriptive compatibility aliases for callers that prefer the adjective first.
validate_bounded_json = validate_json_bounds
ensure_bounded_json = validate_json_bounds

__all__ = [
    "JsonBoundsError",
    "JsonObject",
    "JsonPrimitive",
    "JsonValue",
    "MAX_JSON_BYTES",
    "MAX_JSON_COLLECTION_ITEMS",
    "MAX_JSON_DEPTH",
    "MAX_JSON_NODES",
    "MAX_JSON_URI_LENGTH",
    "ensure_bounded_json",
    "validate_bounded_json",
    "validate_json_bounds",
    "validate_json_schema",
    "validate_resource_uri",
]
