"""16.1.3: every tool Cognita advertises has the shape MCP requires of a Tool.

A client that checks the catalog it is given (the official TypeScript SDK does,
and the SillyTavern MCP client is built on it) refuses the WHOLE `tools/list`
answer when ONE tool is malformed, and is left with no Cognita tools at all.
16.1.0 through 16.1.2 shipped sixteen tools (the audiobook, book-index and
project-file tools) whose `outputSchema` root was only `oneOf`, without the
`"type": "object"` the specification requires; found on 2026-10-10 when a
connected SillyTavern was offered 0 of 73 tools.

These checks follow the Tool definition in the specification's schema
(`inputSchema`/`outputSchema`: `type: "object"`, optional `properties` object
whose values are schema objects, optional `required` list of strings) and run
over the real catalogs, so a tool added later is covered without being listed.
"""

from __future__ import annotations

import pytest

from cognita.proxy import public_tool_catalog, workspace_tool_catalog

_ANNOTATION_TYPES = {
    "title": str, "readOnlyHint": bool, "destructiveHint": bool,
    "idempotentHint": bool, "openWorldHint": bool,
}


def _tools():
    for label, catalog in (("combined", public_tool_catalog()), ("workspace", workspace_tool_catalog())):
        for tool in catalog:
            yield pytest.param(tool, id=f"{label}:{tool.get('name')}")


def _assert_object_root(tool_name: str, key: str, schema) -> None:
    assert isinstance(schema, dict), f"{tool_name}.{key} is not an object"
    assert schema.get("type") == "object", f"{tool_name}.{key} root does not say type: object"
    properties = schema.get("properties", {})
    assert isinstance(properties, dict), f"{tool_name}.{key}.properties is not an object"
    for prop, value in properties.items():
        assert isinstance(value, dict), f"{tool_name}.{key}.properties.{prop} is not a schema object"
    required = schema.get("required", [])
    assert isinstance(required, list) and all(isinstance(item, str) for item in required), (
        f"{tool_name}.{key}.required is not a list of strings"
    )


@pytest.mark.parametrize("tool", _tools())
def test_tool_definition_has_the_shape_mcp_requires(tool):
    name = tool.get("name")
    assert isinstance(name, str) and name
    assert isinstance(tool.get("description", ""), str)
    assert "inputSchema" in tool, f"{name} has no inputSchema"
    _assert_object_root(name, "inputSchema", tool["inputSchema"])
    # Cognita advertises a structured result for every tool.
    assert "outputSchema" in tool, f"{name} has no outputSchema"
    _assert_object_root(name, "outputSchema", tool["outputSchema"])
    annotations = tool.get("annotations", {})
    assert isinstance(annotations, dict)
    for key, expected in _ANNOTATION_TYPES.items():
        if key in annotations:
            assert isinstance(annotations[key], expected), f"{name}.annotations.{key}"


def test_catalogs_are_not_empty():
    assert len(public_tool_catalog()) > 0
    assert len(workspace_tool_catalog()) > 0
