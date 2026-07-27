from tui.commands import parse_command, JumpById, JumpByName, Lock, Unlock, ListCmd, Help, Quit, Unknown


def test_empty_input_returns_unknown():
    cmd = parse_command("")
    assert isinstance(cmd, Unknown)


def test_numeric_input_jumps_by_id():
    cmd = parse_command("3")
    assert isinstance(cmd, JumpById)
    assert cmd.index == 3


def test_jump_command_with_name():
    cmd = parse_command(":jump Alice")
    assert isinstance(cmd, JumpByName)
    assert cmd.name == "Alice"


def test_jump_command_case_insensitive_keyword():
    cmd = parse_command(":JUMP Bob")
    assert isinstance(cmd, JumpByName)
    assert cmd.name == "Bob"


def test_lock_command():
    assert isinstance(parse_command(":lock"), Lock)


def test_unlock_command():
    assert isinstance(parse_command(":unlock"), Unlock)


def test_list_command():
    assert isinstance(parse_command(":list"), ListCmd)


def test_help_command():
    assert isinstance(parse_command(":help"), Help)


def test_quit_command():
    assert isinstance(parse_command(":quit"), Quit)


def test_unknown_command_returns_unknown():
    cmd = parse_command(":fly")
    assert isinstance(cmd, Unknown)
    assert ":fly" in cmd.text


def test_jump_without_name_returns_unknown():
    cmd = parse_command(":jump")
    assert isinstance(cmd, Unknown)


def test_lone_colon_returns_unknown():
    cmd = parse_command(":")
    assert isinstance(cmd, Unknown)
    assert cmd.text == ":"
