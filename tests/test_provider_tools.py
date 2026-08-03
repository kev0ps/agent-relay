from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from agent_relay.json_bounds import (
    MAX_JSON_BYTES,
    MAX_JSON_COLLECTION_ITEMS,
    MAX_JSON_DEPTH,
    validate_json_bounds,
    validate_resource_uri,
)
from agent_relay.output_models import (
    ProviderEmbeddedResource,
    ProviderImageContent,
    ProviderResourceContent,
    ProviderResourceLinkContent,
    ProviderTextContent,
    ProviderToolResult,
)
from agent_relay.protocol import AgentResult
from agent_relay.provider_tools import (
    ProviderToolCatalog,
    ProviderToolDescriptor,
    ProviderToolInvocation,
)


def _descriptor_payload() -> dict[str, object]:
    return {
        "provider_name": "example-provider",
        "tool_name": "read",
        "public_name": "example.read",
        "description": "Read one bounded example value.",
        "input_schema": {
            "type": "object",
            "properties": {"item_id": {"type": "string"}},
            "required": ["item_id"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "title": "Read example"},
        "risk_class": "read_only",
    }


def test_provider_descriptor_is_strict_and_emits_mcp_schema_names() -> None:
    descriptor = ProviderToolDescriptor.model_validate(_descriptor_payload())

    assert descriptor.provider_name == "example-provider"
    assert descriptor.tool_name == "read"
    assert descriptor.public_name == "example.read"
    assert descriptor.model_dump(by_alias=True)["inputSchema"]["type"] == "object"
    assert descriptor.model_dump(by_alias=True)["name"] == "example.read"
    assert descriptor.risk == "read_only"
    assert descriptor.risk_class == "read_only"
    assert descriptor.model_dump(by_alias=True)["risk"] == "read_only"

    with pytest.raises(ValidationError):
        ProviderToolDescriptor.model_validate(_descriptor_payload() | {"handler": "run"})


def test_provider_descriptor_accepts_standard_mcp_aliases_but_no_code_metadata() -> None:
    payload = _descriptor_payload() | {
        "name": "example.read",
        "inputSchema": _descriptor_payload()["input_schema"],
    }
    payload.pop("public_name")
    payload.pop("input_schema")
    descriptor = ProviderToolDescriptor.model_validate(payload)
    assert descriptor.public_name == "example.read"

    with pytest.raises(ValidationError):
        ProviderToolDescriptor.model_validate(
            _descriptor_payload() | {"annotations": {"module": "provider.module"}}
        )
    with pytest.raises(ValidationError):
        ProviderToolDescriptor.model_validate(
            _descriptor_payload()
            | {"input_schema": {"type": "object", "properties": {"code": {}}}}
        )


def test_provider_descriptor_keeps_internal_and_public_names_distinct() -> None:
    descriptor = ProviderToolDescriptor.model_validate(
        _descriptor_payload()
        | {"tool_name": "provider_read", "public_name": "relay.read"}
    )

    assert descriptor.provider_name == "example-provider"
    assert descriptor.tool_name == "provider_read"
    assert descriptor.public_name == "relay.read"
    assert descriptor.name == "relay.read"
    assert descriptor.model_dump(by_alias=True)["name"] == "relay.read"


def test_provider_descriptor_accepts_unambiguous_legacy_name_alias() -> None:
    payload = _descriptor_payload()
    payload.pop("public_name")
    payload["name"] = "example.read"

    descriptor = ProviderToolDescriptor.model_validate(payload)

    assert descriptor.public_name == "example.read"
    assert descriptor.tool_name == "read"


@pytest.mark.parametrize(
    "forbidden_key",
    [
        "execute",
        "handler",
        "module",
        "script",
        "shell",
        "command",
        "x-handler",
        "module.path",
        "command-line",
    ],
)
def test_provider_descriptor_rejects_executable_metadata_keys(forbidden_key: str) -> None:
    with pytest.raises(ValidationError):
        ProviderToolDescriptor.model_validate(
            _descriptor_payload() | {"annotations": {forbidden_key: "blocked"}}
        )


def test_provider_descriptor_bounds_schema_and_risk_class() -> None:
    with pytest.raises(ValidationError):
        ProviderToolDescriptor.model_validate(
            _descriptor_payload() | {"risk_class": "unknown"}
        )

    too_many_properties = {
        "type": "object",
        "properties": {
            f"field_{index}": {"type": "string"}
            for index in range(MAX_JSON_COLLECTION_ITEMS + 1)
        },
    }
    with pytest.raises(ValidationError):
        ProviderToolDescriptor.model_validate(
            _descriptor_payload() | {"input_schema": too_many_properties}
        )

    non_json_schema = _descriptor_payload() | {
        "input_schema": {"type": "object", "default": object()}
    }
    with pytest.raises(ValidationError):
        ProviderToolDescriptor.model_validate(non_json_schema)


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "string", "maxLength": "bad"},
        {"type": "object", "required": "bad", "additionalProperties": False},
        {
            "type": "object",
            "required": ["ok", 1],
            "additionalProperties": False,
        },
    ],
)
def test_provider_descriptor_rejects_invalid_json_schema_keyword_types(
    schema: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="schema"):
        ProviderToolDescriptor.model_validate(_descriptor_payload() | {"input_schema": schema})


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "properties": {"value": {"type": "string"}}},
        {"type": "array", "items": {"type": "string"}},
        {"$ref": "#"},
        {"$ref": "https://schemas.example.test/tool.json"},
    ],
)
def test_provider_descriptor_rejects_semantically_unbounded_json_schemas(
    schema: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="schema"):
        ProviderToolDescriptor.model_validate(_descriptor_payload() | {"input_schema": schema})


@pytest.mark.parametrize(
    "schema",
    [
        {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
        {
            "allOf": [
                {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                }
            ]
        },
    ],
)
def test_provider_descriptor_rejects_unbounded_mapping_json_schemas(
    schema: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="maxProperties"):
        ProviderToolDescriptor.model_validate(_descriptor_payload() | {"input_schema": schema})


def test_provider_descriptor_accepts_bounded_local_definition_and_array_schema() -> None:
    schema = {
        "$defs": {
            "item": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
                "additionalProperties": False,
            }
        },
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {"$ref": "#/$defs/item"},
                "maxItems": 4,
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }

    descriptor = ProviderToolDescriptor.model_validate(
        _descriptor_payload() | {"input_schema": schema}
    )

    assert descriptor.input_schema == schema


def test_provider_invocation_arguments_use_the_shared_json_bounds() -> None:
    invocation = ProviderToolInvocation(
        provider_name="example-provider",
        tool_name="read",
        public_name="example.read",
        arguments={"item_id": "one", "options": {"verbose": True}},
    )
    assert invocation.provider_name == "example-provider"
    assert invocation.tool_name == "read"
    assert invocation.public_name == "example.read"
    assert invocation.model_dump(by_alias=True) == {
        "provider_name": "example-provider",
        "tool_name": "read",
        "name": "example.read",
        "arguments": {"item_id": "one", "options": {"verbose": True}},
    }

    with pytest.raises(ValidationError):
        ProviderToolInvocation(
            provider_name="example-provider",
            tool_name="read",
            public_name="example.read",
            arguments={"not_json": float("nan")},
        )


def test_provider_invocation_enforces_aggregate_json_byte_bound() -> None:
    with pytest.raises(ValidationError, match="invocation JSON exceeds maximum size"):
        ProviderToolInvocation(
            provider_name="example-provider",
            tool_name="read",
            public_name="example.read",
            arguments={"value": "x" * 65_500},
        )


@pytest.mark.parametrize(
    "forbidden_key",
    [
        "execute",
        "handler",
        "module",
        "script",
        "shell",
        "command",
        "x-handler",
        "module.path",
        "command-line",
    ],
)
def test_provider_invocation_rejects_executable_argument_keys(forbidden_key: str) -> None:
    with pytest.raises(ValidationError):
        ProviderToolInvocation(
            provider_name="example-provider",
            tool_name="read",
            public_name="example.read",
            arguments={forbidden_key: "blocked"},
        )


def test_provider_invocation_allows_terminal_command_id_as_provider_data() -> None:
    invocation = ProviderToolInvocation(
        provider_name="example-provider",
        tool_name="read",
        public_name="example.read",
        arguments={"command_id": "pwd"},
    )

    assert invocation.arguments == {"command_id": "pwd"}


def test_provider_catalog_rejects_duplicate_internal_tool_names() -> None:
    first = ProviderToolDescriptor.model_validate(_descriptor_payload())
    second = ProviderToolDescriptor.model_validate(
        _descriptor_payload() | {"public_name": "example.write"}
    )

    with pytest.raises(ValidationError, match="duplicate internal tool name"):
        ProviderToolCatalog(tools=[first, second])


def test_provider_catalog_allows_same_tool_name_for_different_providers() -> None:
    first = ProviderToolDescriptor.model_validate(_descriptor_payload())
    second = ProviderToolDescriptor.model_validate(
        _descriptor_payload()
        | {
            "provider_name": "other-provider",
            "public_name": "other.read",
        }
    )

    catalog = ProviderToolCatalog(tools=[first, second])

    assert [(tool.provider_name, tool.tool_name) for tool in catalog.tools] == [
        ("example-provider", "read"),
        ("other-provider", "read"),
    ]


def test_provider_catalog_rejects_duplicate_public_tool_names() -> None:
    first = ProviderToolDescriptor.model_validate(_descriptor_payload())
    second = ProviderToolDescriptor.model_validate(
        _descriptor_payload() | {"tool_name": "write"}
    )

    with pytest.raises(ValidationError, match="duplicate public tool name"):
        ProviderToolCatalog(tools=[first, second])


def test_provider_catalog_and_invocation_schemas_are_closed() -> None:
    descriptor = ProviderToolDescriptor.model_validate(_descriptor_payload())

    with pytest.raises(ValidationError):
        ProviderToolCatalog(tools=[descriptor], extra=True)
    with pytest.raises(ValidationError):
        ProviderToolInvocation(
            provider_name="example-provider",
            tool_name="read",
            public_name="example.read",
            arguments={},
            execute="blocked",
        )


def test_shared_json_bounds_reject_depth_nodes_and_non_json_values() -> None:
    deep: list[object] = []
    cursor: list[object] = deep
    for _ in range(MAX_JSON_DEPTH):
        child: list[object] = []
        cursor.append(child)
        cursor = child
    with pytest.raises(ValueError, match="deeply nested"):
        validate_json_bounds(deep)

    too_many_nodes: list[object] = [[]]
    for _ in range(12):
        too_many_nodes = [too_many_nodes, too_many_nodes.copy()]
    with pytest.raises(ValueError, match="too many nodes"):
        validate_json_bounds(too_many_nodes)

    with pytest.raises(ValueError, match="JSON values"):
        validate_json_bounds({"value": object()})

    with pytest.raises(ValueError, match="JSON values"):
        validate_json_bounds({"value": math.inf})


def test_provider_tool_result_preserves_bounded_mcp_content() -> None:
    result = ProviderToolResult(
        content=[
            {"type": "text", "text": "hello"},
            {"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"},
        ],
        structuredContent={"value": {"ok": True}},
        isError=False,
    )

    assert result.model_dump(by_alias=True) == {
        "content": [
            {"type": "text", "text": "hello"},
            {"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"},
        ],
        "structuredContent": {"value": {"ok": True}},
        "isError": False,
    }
    assert isinstance(result.content[0], ProviderTextContent)
    assert isinstance(result.content[1], ProviderImageContent)


def test_provider_tool_result_requires_content_and_supports_audio_content() -> None:
    with pytest.raises(ValidationError):
        ProviderToolResult.model_validate({})

    result = ProviderToolResult.model_validate(
        {
            "content": [
                {
                    "type": "audio",
                    "data": "YXVkaW8=",
                    "mimeType": "audio/wav",
                }
            ]
        }
    )

    assert type(result.content[0]).__name__ == "ProviderAudioContent"


def test_provider_result_preserves_native_code_and_uri_data() -> None:
    result = ProviderToolResult(
        content=[{"type": "text", "text": "provider output"}],
        structuredContent={
            "code": "E_PROVIDER",
            "command_id": "pwd",
            "uri": "file:///provider/output",
        },
    )

    assert result.structured_content == {
        "code": "E_PROVIDER",
        "command_id": "pwd",
        "uri": "file:///provider/output",
    }
    assert result.model_dump(by_alias=True)["structuredContent"] == {
        "code": "E_PROVIDER",
        "command_id": "pwd",
        "uri": "file:///provider/output",
    }


def test_provider_tool_result_rejects_unknown_fields_and_untrusted_resources() -> None:
    with pytest.raises(ValidationError):
        ProviderToolResult.model_validate(
            {"content": [{"type": "text", "text": "ok", "code": "x"}]}
        )
    with pytest.raises(ValidationError):
        ProviderToolResult.model_validate(
            {"content": [{"type": "text", "text": "ok"}], "extra": True}
        )
    with pytest.raises(ValidationError):
        ProviderToolResult.model_validate(
            {
                "content": [
                    {
                        "type": "resource",
                        "resource": {"uri": "file:///etc/passwd", "text": "secret"},
                    }
                ]
            }
        )
    with pytest.raises(ValidationError):
        ProviderToolResult.model_validate(
            {
                "content": [
                    {
                        "type": "resource",
                        "resource": {"uri": "https://user:password@example.test/x"},
                    }
                ]
            }
        )


def test_resource_uri_validation_errors_do_not_render_credentials() -> None:
    credential_uri = "https://user:super-secret@example.test/resource?token=top-secret"

    with pytest.raises(ValidationError) as caught:
        ProviderToolResult.model_validate(
            {
                "content": [
                    {
                        "type": "resource",
                        "resource": {"uri": credential_uri, "text": "secret"},
                    }
                ]
            }
        )

    rendered = str(caught.value)
    assert credential_uri not in rendered
    assert "super-secret" not in rendered
    assert "top-secret" not in rendered


def test_provider_resource_content_allows_bounded_public_https_uri() -> None:
    result = ProviderToolResult(
        content=[
            {
                "type": "resource",
                "resource": {
                    "uri": "https://example.test/resource.json",
                    "mimeType": "application/json",
                    "text": "{}",
                },
            }
        ]
    )
    assert isinstance(result.content[0], ProviderResourceContent)
    assert result.model_dump(by_alias=True)["content"][0]["resource"]["uri"].startswith(
        "https://"
    )


def _assert_no_unbounded_additional_properties(schema: object) -> None:
    if isinstance(schema, dict):
        assert schema.get("additionalProperties") is not True
        for value in schema.values():
            _assert_no_unbounded_additional_properties(value)
    elif isinstance(schema, list):
        for value in schema:
            _assert_no_unbounded_additional_properties(value)


def _schema_property_name(model: type[object], field: str) -> str:
    model_field = model.model_fields[field]  # type: ignore[attr-defined]
    return model_field.serialization_alias or model_field.alias or field


@pytest.mark.parametrize(
    ("model", "field"),
    [
        (ProviderToolDescriptor, "input_schema"),
        (ProviderToolDescriptor, "output_schema"),
        (ProviderToolDescriptor, "annotations"),
        (ProviderToolInvocation, "arguments"),
        (ProviderToolResult, "structured_content"),
        (AgentResult, "result"),
    ],
)
def test_generated_json_value_schemas_are_recursive_and_bounded(
    model: type[object], field: str
) -> None:
    schema = model.model_json_schema()  # type: ignore[attr-defined]

    _assert_no_unbounded_additional_properties(schema)
    assert "$defs" in schema
    property_name = _schema_property_name(model, field)
    assert "$ref" in str(schema["properties"][property_name])


def test_provider_content_blocks_preserve_mcp_metadata_aliases() -> None:
    result = ProviderToolResult.model_validate(
        {
            "content": [
                {
                    "type": "text",
                    "text": "hello",
                    "annotations": {"title": "Greeting"},
                    "meta": {"vendor": {"trace": "text"}},
                },
                {
                    "type": "image",
                    "data": "aGVsbG8=",
                    "mimeType": "image/png",
                    "annotations": {"title": "Preview"},
                    "_meta": {"vendor": {"trace": "image"}},
                },
                {
                    "type": "resource",
                    "resource": {
                        "uri": "https://example.test/resource.json",
                        "text": "{}",
                    },
                    "annotations": {"title": "Resource"},
                    "meta": {"vendor": {"trace": "resource"}},
                },
                {
                    "type": "resource_link",
                    "uri": "https://example.test/resource.json",
                    "name": "resource.json",
                    "annotations": {"title": "Link"},
                    "_meta": {"vendor": {"trace": "link"}},
                },
            ]
        }
    )

    assert isinstance(result.content[0], ProviderTextContent)
    assert isinstance(result.content[1], ProviderImageContent)
    assert isinstance(result.content[2], ProviderResourceContent)
    assert isinstance(result.content[3], ProviderResourceLinkContent)
    serialized_content = result.model_dump(by_alias=True)["content"]
    for content in serialized_content:
        assert "annotations" in content
        assert "_meta" in content
        assert "meta" not in content
    assert serialized_content[0]["_meta"]["vendor"]["trace"] == "text"
    assert serialized_content[1]["_meta"]["vendor"]["trace"] == "image"


def test_provider_generated_schemas_publish_wire_aliases_and_required_fields() -> None:
    descriptor_validation = ProviderToolDescriptor.model_json_schema(
        mode="validation", by_alias=True
    )
    descriptor_serialization = ProviderToolDescriptor.model_json_schema(
        mode="serialization", by_alias=True
    )
    for schema in (descriptor_validation, descriptor_serialization):
        properties = schema["properties"]
        assert "name" in properties
        assert "inputSchema" in properties
        assert "outputSchema" in properties
        assert "risk" in properties
        assert "public_name" not in properties
        assert "input_schema" not in properties
        assert "output_schema" not in properties
        assert "risk_class" not in properties

    result_validation = ProviderToolResult.model_json_schema(
        mode="validation", by_alias=True
    )
    result_serialization = ProviderToolResult.model_json_schema(
        mode="serialization", by_alias=True
    )
    for schema in (result_validation, result_serialization):
        assert "content" in schema["required"]
        properties = schema["properties"]
        assert "structuredContent" in properties
        assert "isError" in properties
        assert "_meta" in properties
        assert "structured_content" not in properties
        assert "is_error" not in properties
        assert "meta" not in properties

    image_schema = ProviderImageContent.model_json_schema(
        mode="validation", by_alias=True
    )
    assert "mimeType" in image_schema["properties"]
    assert "mime_type" not in image_schema["properties"]


def test_provider_embedded_resource_schema_requires_exactly_one_body() -> None:
    schema = ProviderEmbeddedResource.model_json_schema(
        mode="validation", by_alias=True
    )

    assert schema["oneOf"] == [
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


def test_provider_embedded_resource_default_serialization_omits_null_body() -> None:
    resource = ProviderEmbeddedResource(
        uri="https://example.test/resource",
        text="hello",
    )

    serialized = resource.model_dump(by_alias=True)

    assert serialized["text"] == "hello"
    assert "blob" not in serialized


def test_provider_embedded_resource_rejects_explicit_null_body() -> None:
    with pytest.raises(ValidationError):
        ProviderEmbeddedResource.model_validate(
            {
                "uri": "https://example.test/resource",
                "text": "hello",
                "blob": None,
            }
        )


def test_provider_content_metadata_stays_within_shared_json_bounds() -> None:
    with pytest.raises(ValidationError):
        ProviderTextContent.model_validate(
            {
                "type": "text",
                "text": "hello",
                "_meta": {"items": list(range(MAX_JSON_COLLECTION_ITEMS + 1))},
            }
        )


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (
            ProviderTextContent,
            {"type": "text", "text": "🚀" * (MAX_JSON_BYTES // 4)},
        ),
        (
            ProviderImageContent,
            {"type": "image", "data": "A" * MAX_JSON_BYTES, "mimeType": "image/png"},
        ),
        (
            ProviderEmbeddedResource,
            {
                "uri": "https://example.test/resource",
                "text": "🚀" * (MAX_JSON_BYTES // 4),
            },
        ),
    ],
)
def test_direct_provider_content_models_enforce_shared_json_byte_bounds(
    model: type[object], payload: dict[str, object]
) -> None:
    with pytest.raises(ValidationError, match="JSON exceeds maximum size"):
        model.model_validate(payload)  # type: ignore[attr-defined]


def _large_bounded_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            f"field_{index}": {"type": "string", "description": "x" * 1400}
            for index in range(24)
        },
        "additionalProperties": False,
    }


def test_provider_descriptor_enforces_aggregate_json_byte_bound() -> None:
    payload = _descriptor_payload()
    payload["input_schema"] = _large_bounded_schema()
    payload["output_schema"] = _large_bounded_schema()
    payload["annotations"] = {"description": "x" * 30000}

    with pytest.raises(ValidationError, match="descriptor JSON exceeds maximum size"):
        ProviderToolDescriptor.model_validate(payload)


def test_provider_catalog_enforces_aggregate_json_byte_bound() -> None:
    first_payload = _descriptor_payload() | {
        "input_schema": _large_bounded_schema(),
        "public_name": "example.first",
    }
    second_payload = _descriptor_payload() | {
        "input_schema": _large_bounded_schema(),
        "provider_name": "other-provider",
        "tool_name": "write",
        "public_name": "other.second",
    }
    first = ProviderToolDescriptor.model_validate(first_payload)
    second = ProviderToolDescriptor.model_validate(second_payload)

    with pytest.raises(ValidationError, match="catalog JSON exceeds maximum size"):
        ProviderToolCatalog(tools=[first, second])


def test_provider_resource_link_requires_mcp_name() -> None:
    with pytest.raises(ValidationError):
        ProviderToolResult.model_validate(
            {
                "content": [
                    {
                        "type": "resource_link",
                        "uri": "https://example.test/resource.json",
                    }
                ]
            }
        )


def test_provider_resource_link_preserves_bounded_official_fields_and_metadata() -> None:
    result = ProviderToolResult.model_validate(
        {
            "content": [
                {
                    "type": "resource_link",
                    "uri": "https://example.test/resource.json",
                    "name": "resource.json",
                    "title": "Example resource",
                    "description": "A provider resource.",
                    "mimeType": "application/json",
                    "size": 42,
                    "icons": [
                        {
                            "src": "https://example.test/icon.png",
                            "mimeType": "image/png",
                            "sizes": "32x32",
                        }
                    ],
                    "_meta": {"vendor": {"trace": "link"}},
                },
                {
                    "type": "resource",
                    "resource": {
                        "uri": "https://example.test/resource.json",
                        "text": "{}",
                        "_meta": {"vendor": {"trace": "embedded"}},
                    },
                },
            ],
            "_meta": {"vendor": {"trace": "result"}},
        }
    )

    serialized = result.model_dump(by_alias=True, exclude_none=True)
    assert serialized["_meta"] == {"vendor": {"trace": "result"}}
    assert serialized["content"][0] == {
        "type": "resource_link",
        "uri": "https://example.test/resource.json",
        "name": "resource.json",
        "title": "Example resource",
        "description": "A provider resource.",
        "mimeType": "application/json",
        "size": 42,
        "icons": [
            {
                "src": "https://example.test/icon.png",
                "mimeType": "image/png",
                "sizes": "32x32",
            }
        ],
        "_meta": {"vendor": {"trace": "link"}},
    }
    assert serialized["content"][1]["resource"]["_meta"] == {
        "vendor": {"trace": "embedded"}
    }


@pytest.mark.parametrize(
    "query",
    [
        "token",
        "token=",
        "access_token",
        "access-token=",
        "api-key=",
        "api_key",
        "authToken=",
        "password",
        "secret=",
        "signature=",
        "client_secret",
        "refresh_token",
        "authorization",
        "bearer",
        "private_key",
        "oauth_token",
        "jwt",
        "client-secret",
        "refresh-token",
        "Authorization",
        "client%5Fsecret",
    ],
)
def test_resource_uri_rejects_credential_like_query_keys_with_blank_values(
    query: str,
) -> None:
    with pytest.raises(ValueError, match="credential-like"):
        validate_resource_uri(f"https://example.test/resource?{query}")


def test_resource_uri_allows_ordinary_query_keys() -> None:
    uri = "https://example.test/resource?page=1&filter=&view=summary"

    assert validate_resource_uri(uri) == uri
