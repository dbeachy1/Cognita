import logging

from cognita.assets.wire import ASSET_TOOL_DEFS
from cognita.proxy import _log_request


def test_client_contract_has_generated_image_shape():
    put = next(tool for tool in ASSET_TOOL_DEFS if tool["name"] == "put_asset")
    image = put["inputSchema"]["properties"]["image"]
    assert image["required"] == ["image_url"]
    assert set(image["properties"]) == {"image_url", "output_hint"}


def test_gateway_never_logs_asset_payload_or_metadata(caplog):
    message = {
        "method": "tools/call",
        "params": {
            "name": "put_asset",
            "arguments": {
                "filepath": "safe.png",
                "operation_id": "private-op",
                "image": {"image_url": "data:image/png;base64," + "SECRETBASE64" * 30},
                "metadata": {"prompts": {"user": "PRIVATE PROMPT"}},
            },
        },
    }
    with caplog.at_level(logging.INFO, logger="cognita.proxy"):
        _log_request("project", message)
    rendered = caplog.text
    assert "SECRETBASE64" not in rendered
    assert "PRIVATE PROMPT" not in rendered
    assert "data:image/png" not in rendered
    assert "image_url_chars" in rendered


def test_gateway_does_not_log_client_supplied_argument_keys_or_tool_names(caplog):
    message = {
        "method": "tools/call",
        "params": {
            "name": "private\nname",
            "arguments": {"PRIVATE DOCUMENT TEXT": "PRIVATE VALUE"},
        },
    }
    with caplog.at_level(logging.INFO, logger="cognita.proxy"):
        _log_request("project", message)
    rendered = caplog.text
    assert "PRIVATE DOCUMENT TEXT" not in rendered
    assert "PRIVATE VALUE" not in rendered
    assert "private\nname" not in rendered
    assert "call <invalid>" in rendered


def test_gateway_does_not_log_invalid_protocol_method(caplog):
    with caplog.at_level(logging.DEBUG, logger="cognita.proxy"):
        _log_request("project", {"method": "private\nrequest text"})
    assert "private\nrequest text" not in caplog.text
    assert "<invalid>" in caplog.text
