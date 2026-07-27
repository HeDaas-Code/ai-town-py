from tui.tile_chars import tile_to_char, TILE_CHARS, AGENT_CHAR, OTHER_AGENT_CHAR


def test_unknown_tile_returns_grass_dot():
    assert tile_to_char(-1) == " "
    assert tile_to_char(999999) == "."


def test_known_tile_returns_mapped_char():
    # Temporarily inject a known mapping
    original = dict(TILE_CHARS)
    try:
        TILE_CHARS[42] = "#"
        assert tile_to_char(42) == "#"
    finally:
        TILE_CHARS.clear()
        TILE_CHARS.update(original)


def test_agent_chars_are_distinct():
    assert AGENT_CHAR == "@"
    assert OTHER_AGENT_CHAR == "o"
    assert AGENT_CHAR != OTHER_AGENT_CHAR
