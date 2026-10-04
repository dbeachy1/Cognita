from cognita.readonly import MUTATING_TOOLS, READONLY_TOOLS, is_tool_allowed_remote


def test_readonly_tools_allowed():
    for tool in READONLY_TOOLS:
        assert is_tool_allowed_remote(tool)


def test_mutating_tools_blocked():
    for tool in MUTATING_TOOLS:
        assert not is_tool_allowed_remote(tool)


def test_unknown_tools_denied_by_default():
    assert not is_tool_allowed_remote("does_not_exist")
    assert not is_tool_allowed_remote("")


def test_no_overlap():
    assert not (READONLY_TOOLS & MUTATING_TOOLS)
