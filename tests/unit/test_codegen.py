from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
import sys
from types import ModuleType
from typing import Any

import pytest
from pydantic import ValidationError

from maco.codegen import _sanitize_identifier, _schema_type_source, _typed_dict_source, generate_from_catalog, generate_sandbox_sdk


def test_generate_sandbox_sdk_uses_tools_package_layout(tmp_path):
    stats = generate_sandbox_sdk(
        {
            "echo-server": [
                {
                    "name": "echo",
                    "description": "Echo a message",
                    "inputSchema": {
                        "type": "object",
                        "required": ["message"],
                        "properties": {"message": {"type": "string"}},
                    },
                    "outputSchema": {"type": "string"},
                }
            ]
        },
        workspace=tmp_path,
    )

    assert stats.tool_count == 1
    tool_source = (tmp_path / "tools" / "echo_server" / "echo.py").read_text(encoding="utf-8")
    assert "from tools._client import call_mcp_tool" in tool_source
    assert "async def echo(arguments: EchoInput) -> EchoOutput:" in tool_source
    assert "return await call_mcp_tool(SERVER_NAME, TOOL_NAME, EchoInput, EchoOutput, arguments)" in tool_source
    init_source = (tmp_path / "tools" / "echo_server" / "__init__.py").read_text(encoding="utf-8")
    assert "from .echo import echo, EchoInput, EchoOutput" in init_source
    manifest = (tmp_path / "manifest.json").read_text(encoding="utf-8")
    assert '"package": "tools"' in manifest
    assert '"module": "tools.echo_server.echo"' in manifest
    assert '"input_schema_hash"' in manifest
    assert '"output_schema_hash"' in manifest


def test_generate_sandbox_sdk_uses_optional_empty_input_and_public_typed_helper(tmp_path):
    generate_sandbox_sdk(
        {
            "utility": [
                {
                    "name": "ping",
                    "description": "Ping the backend",
                    "inputSchema": {"type": "object", "properties": {}},
                    "outputSchema": {"type": "boolean"},
                }
            ]
        },
        workspace=tmp_path,
    )

    tool_source = (tmp_path / "tools" / "utility" / "ping.py").read_text(encoding="utf-8")
    assert "async def ping(arguments: PingInput | None = None) -> PingOutput:" in tool_source
    assert "return await call_mcp_tool(SERVER_NAME, TOOL_NAME, PingInput, PingOutput, arguments)" in tool_source

    client_source = (tmp_path / "tools" / "_client.py").read_text(encoding="utf-8")
    assert "async def _call_mcp_tool(" in client_source
    assert "async def call_mcp_tool(" in client_source
    assert "call_typed_mcp_tool" not in client_source


def test_generated_wrapper_validates_output_schema(tmp_path, monkeypatch):
    generate_from_catalog(
        {
            "echo-server": [
                {
                    "name": "echo",
                    "description": "Echo a message",
                    "inputSchema": {
                        "type": "object",
                        "required": ["message"],
                        "properties": {"message": {"type": "string"}},
                    },
                    "outputSchema": {
                        "type": "object",
                        "required": ["result"],
                        "properties": {"result": {"type": "string"}},
                    },
                }
            ]
        },
        workspace=tmp_path,
        package_name="typed_tools",
        client_module="typed_tools._client",
    )

    sys.path.insert(0, str(tmp_path))
    try:
        client_module = importlib.import_module("typed_tools._client")
        echo_module = importlib.import_module("typed_tools.echo_server.echo")

        async def invalid_output(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {}

        async def valid_output(*_args: Any, **_kwargs: Any) -> dict[str, str]:
            return {"result": "hello"}

        monkeypatch.setattr(client_module, "_call_mcp_tool", invalid_output)
        with pytest.raises(ValidationError):
            asyncio.run(echo_module.echo(echo_module.EchoInput(message="hello")))

        monkeypatch.setattr(client_module, "_call_mcp_tool", valid_output)
        assert asyncio.run(echo_module.echo(echo_module.EchoInput(message="hello"))).result == "hello"
    finally:
        sys.path.remove(str(tmp_path))
        for module_name in list(sys.modules):
            if module_name == "typed_tools" or module_name.startswith("typed_tools."):
                del sys.modules[module_name]


def test_sanitize_identifier():
    assert _sanitize_identifier("read_file") == "read_file"
    assert _sanitize_identifier("browser-click") == "browser_click"
    assert _sanitize_identifier("listCommits") == "list_commits"
    assert _sanitize_identifier("123 list") == "_123_list"
    assert _sanitize_identifier("class") == "class_"


def test_typed_dict_source_uses_json_property_names():
    source = _typed_dict_source(
        "Input",
        {
            "type": "object",
            "required": ["path-name"],
            "properties": {
                "path-name": {"type": "string"},
                "recursive": {"type": "boolean"},
            },
        },
    )

    assert "class Input(BaseModel):" in source
    assert "path_name: str = Field(default=..., alias='path-name')" in source
    assert "recursive: bool | None = Field(default=None)" in source
    compile(
        "import typing as _t\nfrom pydantic import BaseModel, ConfigDict, Field\n"
        + source,
        str(Path("generated.py")),
        "exec",
    )


def test_generated_models_validate_nested_aliases():
    typed = _schema_type_source(
        "SearchInput",
        {
            "type": "object",
            "required": ["query-text", "filters"],
            "properties": {
                "query-text": {"type": "string"},
                "filters": {
                    "type": "object",
                    "required": ["max-results"],
                    "properties": {
                        "max-results": {"type": "integer", "minimum": 1},
                        "include-archived": {"type": "boolean"},
                    },
                },
            },
        },
    )
    namespace = _exec_generated_source(typed.source)

    search_input = namespace["SearchInput"].model_validate(
        {
            "query-text": "mcp",
            "filters": {"max-results": 5},
        }
    )

    assert search_input.query_text == "mcp"
    assert search_input.filters.max_results == 5
    assert search_input.model_dump(by_alias=True, exclude_none=True) == {
        "query-text": "mcp",
        "filters": {"max-results": 5},
    }


def test_schema_type_source_generates_typed_output_aliases():
    typed = _schema_type_source(
        "SearchOutput",
        {
            "type": "object",
            "required": ["items"],
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["title", "score"],
                        "properties": {
                            "title": {"type": "string"},
                            "score": {"type": "number"},
                            "tags": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                },
                "status": {"enum": ["ok", "partial"]},
            },
        },
    )

    assert typed.type_expr == "SearchOutput"
    assert typed.is_model
    assert "class SearchOutputItemsItem(BaseModel):" in typed.source
    assert "items: list[SearchOutputItemsItem] = Field(default=...)" in typed.source
    assert "status: _t.Literal['ok', 'partial'] | None = Field(default=None)" in typed.source
    compile(
        "import typing as _t\nfrom pydantic import BaseModel, ConfigDict, Field\n"
        + typed.source,
        str(Path("generated.py")),
        "exec",
    )


def test_schema_type_source_generates_native_alias_for_scalar_schema():
    typed = _schema_type_source("CountOutput", {"type": "integer"})

    assert typed.type_expr == "CountOutput"
    assert not typed.is_model
    assert "CountOutput = int" in typed.source
    compile(
        "import typing as _t\nfrom pydantic import BaseModel, ConfigDict, Field\n"
        + typed.source,
        str(Path("generated.py")),
        "exec",
    )


def test_schema_type_source_forbids_extra_fields_when_additional_properties_false():
    typed = _schema_type_source(
        "StrictInput",
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["name"],
            "properties": {"name": {"type": "string"}},
        },
    )
    namespace = _exec_generated_source(typed.source)

    with pytest.raises(ValidationError):
        namespace["StrictInput"].model_validate({"name": "ok", "extra": True})


def test_schema_type_source_merges_all_of_object_properties():
    typed = _schema_type_source(
        "MergedInput",
        {
            "allOf": [
                {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}},
                {"type": "object", "required": ["b"], "properties": {"b": {"type": "integer"}}},
            ]
        },
    )
    namespace = _exec_generated_source(typed.source)
    value = namespace["MergedInput"].model_validate({"a": "x", "b": 1})

    assert value.a == "x"
    assert value.b == 1


def test_generated_models_avoid_reserved_field_names():
    typed = _schema_type_source(
        "ReservedInput",
        {
            "type": "object",
            "properties": {
                "model_dump": {"type": "string"},
                "schema": {"type": "integer"},
            },
        },
    )
    namespace = _exec_generated_source(typed.source)
    value = namespace["ReservedInput"].model_validate({"model_dump": "x", "schema": 3})

    assert value.field_model_dump == "x"
    assert value.field_schema == 3
    assert value.model_dump(by_alias=True, exclude_none=True) == {"model_dump": "x", "schema": 3}


def test_generated_models_avoid_private_attribute_field_names():
    typed = _schema_type_source(
        "PrivateInput",
        {
            "type": "object",
            "properties": {
                "123-name": {"type": "string"},
            },
        },
    )
    namespace = _exec_generated_source(typed.source)
    value = namespace["PrivateInput"].model_validate({"123-name": "x"})

    assert value.field_123_name == "x"
    assert value.model_dump(by_alias=True, exclude_none=True) == {"123-name": "x"}


def _exec_generated_source(source: str) -> dict[str, Any]:
    module = ModuleType("generated_test_models")
    namespace = module.__dict__
    sys.modules[module.__name__] = module
    exec(
        "import typing as _t\nfrom pydantic import BaseModel, ConfigDict, Field\n"
        + source,
        namespace,
    )
    return namespace
