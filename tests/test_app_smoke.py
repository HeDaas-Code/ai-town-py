import pytest

from tui.app import TownApp


class FakeGame:
    """Minimal game stub for app composition testing."""
    class _World:
        def __init__(self):
            self.players = {}
            self.conversations = {}
            self.agents = {}
    class _Router:
        def add_observer(self, obs):
            pass
    def __init__(self):
        self.world = FakeGame._World()
        self.world_map = type("WM", (), {"width": 10, "height": 10, "bg_tiles": [], "object_tiles": []})()
        self.dialogue_router = FakeGame._Router()
        self.player_descriptions = {}
        self.agent_descriptions = {}


@pytest.mark.asyncio
async def test_app_composes_without_error():
    app = TownApp(FakeGame())
    async with app.run_test() as pilot:
        await pilot.pause()
        # No exception means composition + initial render worked
        assert app.selected_player_id is None


@pytest.mark.asyncio
async def test_app_handles_quit_command():
    app = TownApp(FakeGame())
    async with app.run_test() as pilot:
        app.command_bar.value = ":quit"
        await pilot.press("enter")
        await pilot.pause()
        # App should be exiting
        assert app._should_exit
