from cognita.assets.wire import ASSET_TOOL_DEFS, ASSET_TOOL_NAMES, image_result


def test_asset_wire_is_closed_and_complete():
    assert ASSET_TOOL_NAMES == {
        "put_asset", "update_asset_metadata", "search_assets", "list_assets",
        "get_asset_info", "get_asset", "reindex_assets", "ocr_asset", "remove_asset",
    }
    for tool in ASSET_TOOL_DEFS:
        assert tool["inputSchema"]["additionalProperties"] is False
    put = next(tool for tool in ASSET_TOOL_DEFS if tool["name"] == "put_asset")
    assert put["inputSchema"]["properties"]["expected_received_size"]["maximum"] == 16 * 1_048_576
    assert put["inputSchema"]["properties"]["image"]["properties"]["image_url"]["maxLength"] == (
        len("data:image/png;base64,") + 22_369_624
    )


def test_get_asset_result_has_separate_image_block():
    result = image_result({"status": "success"}, b"png")
    assert [block["type"] for block in result["content"]] == ["text", "image"]
    assert "cG5n" not in result["content"][0]["text"]
