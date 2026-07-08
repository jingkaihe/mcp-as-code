"""Generate Python code interfaces for MCP tools."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import keyword
from pathlib import Path
import re
import shutil
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from jinja2 import Environment, PackageLoader, StrictUndefined

from . import __version__
from .config import MacoConfig
from .mcp_manager import MCPManager


_CODEGEN_TEMPLATES = Environment(
    loader=PackageLoader("maco", "templates"),
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
    undefined=StrictUndefined,
)


def _pyrepr(value: Any) -> str:
    return repr(value)


_CODEGEN_TEMPLATES.filters["pyrepr"] = _pyrepr


PYDANTIC_RESERVED_FIELD_NAMES = {
    "construct",
    "copy",
    "dict",
    "from_orm",
    "json",
    "model_computed_fields",
    "model_config",
    "model_construct",
    "model_copy",
    "model_dump",
    "model_dump_json",
    "model_extra",
    "model_fields",
    "model_fields_set",
    "model_json_schema",
    "model_parametrized_name",
    "model_post_init",
    "model_rebuild",
    "model_validate",
    "model_validate_json",
    "model_validate_strings",
    "parse_file",
    "parse_obj",
    "parse_raw",
    "schema",
    "schema_json",
    "update_forward_refs",
    "validate",
}


@dataclass(frozen=True)
class GenerationStats:
    server_count: int
    tool_count: int
    workspace: Path


@dataclass(frozen=True)
class TypeSource:
    """Generated Python type source for one JSON schema."""

    source: str
    type_expr: str
    is_model: bool = False


@dataclass(frozen=True)
class ToolExport:
    function: str
    input_type: str
    output_type: str


async def generate_async(
    config: MacoConfig,
    workspace: str | Path = ".maco",
    server_filter: str | None = None,
    clean: bool = False,
) -> GenerationStats:
    """Generate Python wrappers for all configured MCP tools."""

    async with MCPManager(config) as manager:
        tools_by_server = await manager.list_tools(server_filter=server_filter)

    return generate_from_catalog(
        tools_by_server,
        workspace=workspace,
        clean=clean,
        config_path=config.path,
    )


def generate_from_catalog(
    tools_by_server: dict[str, list[dict[str, Any]]],
    *,
    workspace: str | Path = ".maco",
    clean: bool = False,
    config_path: str | Path | None = None,
    package_name: str = "maco_generated.servers",
    client_module: str = "maco_generated.client",
    package_docstring: str = "Generated MCP wrappers for maco.",
    servers_docstring: str = "Generated MCP server packages.",
) -> GenerationStats:
    """Generate wrappers from an already-fetched MCP tool catalog."""

    workspace_path = Path(workspace).expanduser().resolve()
    if clean and workspace_path.exists():
        shutil.rmtree(workspace_path)

    package_parts = package_name.split(".")
    if not package_parts or any(not part for part in package_parts):
        raise ValueError("package_name must be a dotted Python package name")

    client_parts = client_module.split(".")
    if not client_parts or any(not part for part in client_parts):
        raise ValueError("client_module must be a dotted Python module name")

    generated_pkg = workspace_path / package_parts[0]
    servers_pkg = workspace_path.joinpath(*package_parts)
    servers_pkg.mkdir(parents=True, exist_ok=True)

    _write_workspace_pyproject(workspace_path)
    _write_template(
        generated_pkg / "__init__.py",
        "codegen/package_init.py.j2",
        docstring=package_docstring,
    )
    for depth in range(1, max(len(package_parts) - 1, 1)):
        package_path = workspace_path.joinpath(*package_parts[: depth + 1])
        _write_template(
            package_path / "__init__.py",
            "codegen/package_init.py.j2",
            docstring=servers_docstring,
        )
    if servers_pkg != generated_pkg:
        _write_template(
            servers_pkg / "__init__.py",
            "codegen/package_init.py.j2",
            docstring=servers_docstring,
        )
    (generated_pkg / "py.typed").write_text("", encoding="utf-8")
    client_path = workspace_path.joinpath(*client_parts).with_suffix(".py")
    _write_client(client_path)

    manifest = {
        "version": 2,
        "generator": "maco.codegen",
        "generator_version": __version__ or "unknown",
        "config": str(config_path) if config_path is not None else None,
        "config_hash": _file_hash(config_path) if config_path is not None else None,
        "package": package_name,
        "servers": [],
    }

    server_module_names = _unique_sanitized_names(tools_by_server.keys())
    server_count = 0
    tool_count = 0

    for server_name, tools in sorted(tools_by_server.items()):
        server_module = server_module_names[server_name]
        server_dir = servers_pkg / server_module
        server_dir.mkdir(parents=True, exist_ok=True)
        tool_module_names = _unique_sanitized_names(tool["name"] for tool in tools)

        exports: list[ToolExport] = []
        server_manifest = {
            "name": server_name,
            "module": server_module,
            "tools": [],
        }
        for tool in sorted(tools, key=lambda item: item["name"]):
            tool_name = tool["name"]
            func_name = tool_module_names[tool_name]
            module_path = server_dir / f"{func_name}.py"
            tool_export = _write_tool(module_path, server_name, tool, func_name, client_module)
            exports.append(tool_export)
            server_manifest["tools"].append(
                {
                    "name": tool_name,
                    "function": func_name,
                    "module": f"{package_name}.{server_module}.{func_name}",
                    "description": tool.get("description") or "",
                    "input_type": tool_export.input_type,
                    "output_type": tool_export.output_type,
                    "input_schema_hash": _schema_hash(tool.get("inputSchema")),
                    "output_schema_hash": _schema_hash(tool.get("outputSchema")),
                }
            )
            tool_count += 1

        _write_server_init(server_dir / "__init__.py", exports)
        manifest["servers"].append(server_manifest)
        server_count += 1

    (workspace_path / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    return GenerationStats(
        server_count=server_count,
        tool_count=tool_count,
        workspace=workspace_path,
    )


def generate(
    config: MacoConfig,
    workspace: str | Path = ".maco",
    server_filter: str | None = None,
    clean: bool = False,
) -> GenerationStats:
    return asyncio.run(generate_async(config, workspace, server_filter, clean))


def generate_sandbox_sdk(
    tools_by_server: dict[str, list[dict[str, Any]]],
    *,
    workspace: str | Path,
    clean: bool = True,
) -> GenerationStats:
    """Generate the sandbox-facing SDK package at ``tools.<server>``."""

    return generate_from_catalog(
        tools_by_server,
        workspace=workspace,
        clean=clean,
        package_name="tools",
        client_module="tools._client",
        package_docstring="Generated sandbox tools for maco.",
        servers_docstring="Generated sandbox tool modules.",
    )


def generate_sandbox_sdk_from_gateway(
    gateway_url: str,
    *,
    token: str | None = None,
    workspace: str | Path,
    clean: bool = True,
    timeout: float | None = 30.0,
) -> GenerationStats:
    """Generate the sandbox SDK from a running gateway's live tool catalog."""

    return generate_sandbox_sdk(
        fetch_gateway_tools(gateway_url, token=token, timeout=timeout),
        workspace=workspace,
        clean=clean,
    )


def fetch_gateway_tools(
    gateway_url: str,
    *,
    token: str | None = None,
    timeout: float | None = 30.0,
) -> dict[str, list[dict[str, Any]]]:
    """Fetch the live tool catalog from a running maco gateway."""

    url = gateway_url.rstrip("/") + "/tools"
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"failed to fetch maco gateway tools: HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"failed to connect to maco gateway at {url}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("servers"), dict):
        raise RuntimeError("maco gateway /tools response must contain a servers object")
    result: dict[str, list[dict[str, Any]]] = {}
    for server_name, tools in payload["servers"].items():
        if not isinstance(server_name, str) or not isinstance(tools, list):
            raise RuntimeError("maco gateway /tools response has an invalid server entry")
        server_tools: list[dict[str, Any]] = []
        for tool in tools:
            if not isinstance(tool, dict):
                raise RuntimeError("maco gateway /tools response has an invalid tool entry")
            server_tools.append(tool)
        result[server_name] = server_tools
    return result


def server_module_names(server_names: Any) -> dict[str, str]:
    """Return generated module names for configured MCP server names."""

    return _unique_sanitized_names(server_names)


def _render_template(template_name: str, **context: Any) -> str:
    return _CODEGEN_TEMPLATES.get_template(template_name).render(**context)


def _render_source(template_name: str, **context: Any) -> str:
    return _render_template(template_name, **context).rstrip()


def _write_template(path: Path, template_name: str, **context: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_template(template_name, **context), encoding="utf-8")


def _write_workspace_pyproject(workspace: Path) -> None:
    _write_template(workspace / "pyproject.toml", "codegen/pyproject.toml.j2")


def _write_client(path: Path) -> None:
    _write_template(path, "codegen/client.py.j2")


def _write_tool(
    path: Path,
    server_name: str,
    tool: dict[str, Any],
    func_name: str,
    client_module: str,
) -> ToolExport:
    tool_name = tool["name"]
    description = tool.get("description") or ""
    input_schema = tool.get("inputSchema") or {"type": "object", "properties": {}}
    output_schema = tool.get("outputSchema")
    input_type = _schema_type_source(f"{_class_name(func_name)}Input", input_schema)
    output_type = _schema_type_source(
        f"{_class_name(func_name)}Output",
        output_schema,
        missing_type_expr="_t.Any",
    )
    _write_template(
        path,
        "codegen/tool.py.j2",
        description=description,
        docstring=_docstring(description, input_schema, output_schema),
        func_name=func_name,
        input_default_suffix=_input_default_suffix(input_schema),
        input_type_expr=input_type.type_expr,
        input_type_source=input_type.source,
        output_type_expr=output_type.type_expr,
        output_type_source=output_type.source,
        client_module=client_module,
        server_name=server_name,
        tool_name=tool_name,
    )

    return ToolExport(
        function=func_name,
        input_type=input_type.type_expr,
        output_type=output_type.type_expr,
    )


def _write_server_init(path: Path, exports: list[ToolExport]) -> None:
    _write_template(path, "codegen/server_init.py.j2", exports=exports)


def _typed_dict_source(class_name: str, schema: dict[str, Any]) -> str:
    """Backward-compatible helper used by tests and older callers."""

    return _schema_type_source(class_name, schema).source


def _schema_type_source(
    root_name: str,
    schema: Any,
    *,
    missing_type_expr: str = "dict[str, _t.Any]",
) -> TypeSource:
    if not isinstance(schema, dict):
        root_type = _class_name(root_name)
        return TypeSource(_render_type_alias(root_type, missing_type_expr), root_type)
    used_names: set[str] = set()
    return _schema_to_type(_class_name(root_name), schema, schema, used_names, define_named=True)


def _schema_to_type(
    type_name: str,
    schema: dict[str, Any],
    root_schema: dict[str, Any],
    used_names: set[str],
    *,
    define_named: bool = False,
) -> TypeSource:
    schema = _resolve_schema_ref(schema, root_schema)

    all_of_schema = _merged_all_of_schema(schema, root_schema)
    if all_of_schema is not None:
        return _schema_to_type(type_name, all_of_schema, root_schema, used_names, define_named=define_named)

    if "const" in schema:
        return _maybe_alias(type_name, _literal_type([schema["const"]]), used_names, define_named, schema=schema)
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        return _maybe_alias(type_name, _literal_type(schema["enum"]), used_names, define_named, schema=schema)

    for key in ("oneOf", "anyOf"):
        variants = schema.get(key)
        if isinstance(variants, list) and variants:
            definitions: list[str] = []
            type_exprs: list[str] = []
            for index, variant in enumerate(variants, start=1):
                if not isinstance(variant, dict):
                    type_exprs.append("_t.Any")
                    continue
                variant_schema = cast("dict[str, Any]", variant)
                variant_type = _schema_to_type(
                    f"{type_name}Variant{index}",
                    variant_schema,
                    root_schema,
                    used_names,
                )
                definitions.append(variant_type.source)
                type_exprs.append(variant_type.type_expr)
            return _maybe_alias(
                type_name,
                _union_type(type_exprs),
                used_names,
                define_named,
                definitions,
            )

    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        definitions = []
        type_exprs = []
        for item in schema_type:
            item_schema = {**schema, "type": item}
            item_type = _schema_to_type(type_name, item_schema, root_schema, used_names)
            definitions.append(item_type.source)
            type_exprs.append(item_type.type_expr)
        return _maybe_alias(
            type_name,
            _union_type(type_exprs),
            used_names,
            define_named,
            definitions,
        )

    if schema_type == "object" or "properties" in schema:
        return _object_type_source(type_name, schema, root_schema, used_names, define_named=define_named)
    if schema_type == "array":
        items = schema.get("items")
        if isinstance(items, dict):
            item_type = _schema_to_type(
                f"{type_name}Item",
                items,
                root_schema,
                used_names,
            )
            return _maybe_alias(
                type_name,
                f"list[{item_type.type_expr}]",
                used_names,
                define_named,
                [item_type.source],
                schema=schema,
            )
        return _maybe_alias(type_name, "list[_t.Any]", used_names, define_named, schema=schema)
    if schema_type == "string":
        return _maybe_alias(type_name, "str", used_names, define_named, schema=schema)
    if schema_type == "integer":
        return _maybe_alias(type_name, "int", used_names, define_named, schema=schema)
    if schema_type == "number":
        return _maybe_alias(type_name, "float", used_names, define_named, schema=schema)
    if schema_type == "boolean":
        return _maybe_alias(type_name, "bool", used_names, define_named, schema=schema)
    if schema_type == "null":
        return _maybe_alias(type_name, "None", used_names, define_named)
    return _maybe_alias(type_name, "_t.Any", used_names, define_named)


def _object_type_source(
    type_name: str,
    schema: dict[str, Any],
    root_schema: dict[str, Any],
    used_names: set[str],
    *,
    define_named: bool,
) -> TypeSource:
    properties = schema.get("properties")
    additional = schema.get("additionalProperties")
    if (not properties) and isinstance(additional, dict):
        value_type = _schema_to_type(f"{type_name}Value", additional, root_schema, used_names)
        return _maybe_alias(
            type_name,
            f"dict[str, {value_type.type_expr}]",
            used_names,
            define_named,
            [value_type.source],
            schema=schema,
        )

    if isinstance(properties, dict):
        reserved_name = _reserve_type_name(type_name, used_names)
        required = {field for field in schema.get("required", []) if isinstance(field, str)}
        definitions: list[str] = []
        fields: list[dict[str, str]] = []
        used_fields: set[str] = set()
        for raw_prop_name, raw_prop_schema in sorted(properties.items()):
            prop_name = str(raw_prop_name)
            prop_schema = cast("dict[str, Any]", raw_prop_schema if isinstance(raw_prop_schema, dict) else {})
            prop_type = _schema_to_type(
                f"{reserved_name}{_class_name(str(prop_name))}",
                prop_schema,
                root_schema,
                used_names,
            )
            definitions.append(prop_type.source)
            default = _field_default(prop_name, prop_schema, required)
            nullable = _is_nullable(prop_schema)
            type_expr = prop_type.type_expr
            if prop_name not in required or nullable:
                type_expr = _optional_type(type_expr)
            field_name = _safe_field_name(prop_name, used_fields)
            field_args = _field_args(prop_name, prop_schema, default, field_name)
            fields.append(
                {
                    "field_args": field_args,
                    "name": field_name,
                    "type_expr": type_expr,
                }
            )
        definitions.append(
            _render_source(
                "codegen/model.py.j2",
                class_name=reserved_name,
                extra_behavior=_extra_behavior(schema),
                fields=fields,
            )
        )
        return TypeSource(_join_definitions(definitions), reserved_name, is_model=True)

    return _maybe_alias(type_name, "dict[str, _t.Any]", used_names, define_named)


def _merged_all_of_schema(schema: dict[str, Any], root_schema: dict[str, Any]) -> dict[str, Any] | None:
    all_of = schema.get("allOf")
    if not isinstance(all_of, list) or not all_of:
        return None

    merged = {key: value for key, value in schema.items() if key != "allOf"}
    properties: dict[str, Any] = dict(merged.get("properties") or {}) if isinstance(merged.get("properties"), dict) else {}
    required: list[str] = [item for item in merged.get("required", []) if isinstance(item, str)]
    additional_properties = merged.get("additionalProperties")

    for item in all_of:
        if not isinstance(item, dict):
            return None
        item_schema = _resolve_schema_ref(item, root_schema)
        if not _is_object_schema(item_schema):
            return item_schema if len(all_of) == 1 and not properties else None
        item_properties = item_schema.get("properties")
        if isinstance(item_properties, dict):
            properties.update(item_properties)
        for required_field in item_schema.get("required", []):
            if isinstance(required_field, str) and required_field not in required:
                required.append(required_field)
        item_additional = item_schema.get("additionalProperties")
        if item_additional is False:
            additional_properties = False
        elif additional_properties is None and item_additional is not None:
            additional_properties = item_additional

    merged["type"] = "object"
    merged["properties"] = properties
    if required:
        merged["required"] = required
    if additional_properties is not None:
        merged["additionalProperties"] = additional_properties
    return merged


def _is_object_schema(schema: dict[str, Any]) -> bool:
    return schema.get("type") == "object" or "properties" in schema


def _extra_behavior(schema: dict[str, Any]) -> str:
    return "forbid" if schema.get("additionalProperties") is False else "allow"


def _resolve_schema_ref(schema: dict[str, Any], root_schema: dict[str, Any]) -> dict[str, Any]:
    ref = schema.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return schema
    target: Any = root_schema
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(target, dict) or part not in target:
            return schema
        target = target[part]
    if not isinstance(target, dict):
        return schema
    merged = dict(target)
    merged.update({key: value for key, value in schema.items() if key != "$ref"})
    return merged


def _maybe_alias(
    type_name: str,
    type_expr: str,
    used_names: set[str],
    define_named: bool,
    definitions: list[str] | None = None,
    *,
    schema: dict[str, Any] | None = None,
) -> TypeSource:
    definitions = definitions or []
    if not define_named:
        return TypeSource(_join_definitions(definitions), type_expr)
    reserved_name = _reserve_type_name(type_name, used_names)
    type_expr = _annotated_type(type_expr, schema)
    return TypeSource(_join_definitions([*definitions, _render_type_alias(reserved_name, type_expr)]), reserved_name)


def _render_type_alias(type_name: str, type_expr: str) -> str:
    return _render_source("codegen/type_alias.py.j2", type_name=type_name, type_expr=type_expr)


def _field_default(prop_name: str, schema: dict[str, Any], required: set[str]) -> str:
    if "default" in schema:
        return repr(schema["default"])
    return "..." if prop_name in required else "None"


def _field_args(prop_name: str, schema: dict[str, Any], default: str, field_name: str) -> str:
    kwargs = [f"default={default}"]
    if field_name != prop_name:
        kwargs.append(f"alias={prop_name!r}")
    description = schema.get("description")
    if isinstance(description, str) and description:
        kwargs.append(f"description={description!r}")
    title = schema.get("title")
    if isinstance(title, str) and title:
        kwargs.append(f"title={title!r}")
    kwargs.extend(_constraint_args(schema))
    return f"Field({', '.join(kwargs)})"


def _safe_field_name(name: str, used_fields: set[str]) -> str:
    candidate = _sanitize_identifier(name)
    if candidate.startswith("_"):
        candidate = f"field_{candidate.lstrip('_') or 'value'}"
    if candidate in PYDANTIC_RESERVED_FIELD_NAMES:
        candidate = f"field_{candidate}"
    base = candidate
    index = 2
    while candidate in used_fields:
        candidate = f"{base}_{index}"
        index += 1
    used_fields.add(candidate)
    return candidate


def _optional_type(type_expr: str) -> str:
    if "None" in type_expr.split(" | "):
        return type_expr
    return f"{type_expr} | None"


def _is_nullable(schema: dict[str, Any]) -> bool:
    schema_type = schema.get("type")
    return schema_type == "null" or (isinstance(schema_type, list) and "null" in schema_type)


def _literal_type(values: list[Any]) -> str:
    return "_t.Literal[{}]".format(", ".join(repr(value) for value in values))


def _union_type(type_exprs: list[str]) -> str:
    unique = []
    for expr in type_exprs:
        if expr and expr not in unique:
            unique.append(expr)
    if not unique:
        return "_t.Any"
    if len(unique) == 1:
        return unique[0]
    return " | ".join(unique)


def _join_definitions(definitions: list[str]) -> str:
    return "\n\n".join(definition for definition in definitions if definition)


def _reserve_type_name(type_name: str, used_names: set[str]) -> str:
    base = _class_name(type_name)
    candidate = base
    index = 2
    while candidate in used_names:
        candidate = f"{base}{index}"
        index += 1
    used_names.add(candidate)
    return candidate


def _docstring(description: str, input_schema: dict[str, Any], output_schema: Any) -> str:
    del input_schema, output_schema
    return (description.strip() or "Call the MCP tool.").replace('"""', '\"\"\"')


def _unique_sanitized_names(names: Any) -> dict[str, str]:
    originals = [str(name) for name in names]
    groups: dict[str, list[str]] = {}
    for original in originals:
        groups.setdefault(_sanitize_identifier(original), []).append(original)
    result: dict[str, str] = {}
    for base, group in groups.items():
        if len(group) == 1:
            result[group[0]] = base
            continue
        for index, original in enumerate(sorted(group), start=1):
            result[original] = base if index == 1 else f"{base}_{index}"
    return result


def _sanitize_identifier(name: str) -> str:
    words = [word for part in re.split(r"[^0-9A-Za-z]+", name.strip()) for word in _identifier_words(part)]
    if not words:
        result = "tool"
    else:
        result = "_".join(part.lower() for part in words)
    result = re.sub(r"\W", "_", result)
    if result[0].isdigit():
        result = f"_{result}"
    if keyword.iskeyword(result):
        result += "_"
    return result


def _identifier_words(part: str) -> list[str]:
    return re.findall(r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|\d+", part)


def _input_default_suffix(schema: Any) -> str:
    if not isinstance(schema, dict):
        return " = None"
    schema = _resolve_schema_ref(schema, schema)
    if not _is_object_schema(schema):
        return ""
    required = schema.get("required")
    if isinstance(required, list) and any(isinstance(item, str) for item in required):
        return ""
    return " | None = None"


def _annotated_type(type_expr: str, schema: dict[str, Any] | None) -> str:
    if schema is None:
        return type_expr
    constraints = _constraint_args(schema)
    if not constraints:
        return type_expr
    return f"_t.Annotated[{type_expr}, Field({', '.join(constraints)})]"


def _constraint_args(schema: dict[str, Any]) -> list[str]:
    args: list[str] = []
    for schema_key, field_key in (
        ("minimum", "ge"),
        ("maximum", "le"),
        ("exclusiveMinimum", "gt"),
        ("exclusiveMaximum", "lt"),
        ("multipleOf", "multiple_of"),
        ("minLength", "min_length"),
        ("maxLength", "max_length"),
        ("minItems", "min_length"),
        ("maxItems", "max_length"),
        ("minProperties", "min_length"),
        ("maxProperties", "max_length"),
        ("pattern", "pattern"),
    ):
        if schema_key in schema:
            args.append(f"{field_key}={schema[schema_key]!r}")

    json_schema_extra = {key: schema[key] for key in ("format", "uniqueItems") if key in schema}
    if json_schema_extra:
        args.append(f"json_schema_extra={json_schema_extra!r}")
    return args


def _schema_hash(schema: Any) -> str | None:
    if schema is None:
        return None
    encoded = json.dumps(schema, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_hash(path: str | Path | None) -> str | None:
    if path is None:
        return None
    try:
        data = Path(path).expanduser().read_bytes()
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest()


def _class_name(func_name: str) -> str:
    parts = [part for part in re.split(r"[^0-9A-Za-z]+", str(func_name)) if part]
    result = "".join(part[:1].upper() + part[1:] for part in parts) or "Tool"
    if result[0].isdigit():
        result = f"_{result}"
    if keyword.iskeyword(result):
        result += "Type"
    return result
