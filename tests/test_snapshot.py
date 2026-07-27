from engine.types import Point, Vector
from engine.player import Player, Activity
from engine.agent import Agent
from engine.world import World
from engine.conversation import Conversation
from tui.snapshot import build_snapshot, AgentSnapshot


def _make_game_with_players(players, conversations=None, agent_descriptions=None):
    class FakeGame:
        def __init__(self):
            self.world = World(players={p.id: p for p in players},
                               conversations=conversations or {})
            self.player_descriptions = {}
            self.agent_descriptions = agent_descriptions or {}
            # map agents by player_id for the snapshot
            self.world.agents = {}
            for a in getattr(self, "_agents", []):
                self.world.agents[a.id] = a
    g = FakeGame()
    return g


def test_snapshot_for_idle_player_no_pathfinding_no_activity():
    p = Player(id="p:0", position=Point(5, 3), facing=Vector(1, 0), last_input=0)
    a = Agent(id="a:0", player_id="p:0")
    g = _make_game_with_players([p])
    g._agents = [a]
    g.world.agents = {a.id: a}
    snap = build_snapshot(g, "p:0")
    assert isinstance(snap, AgentSnapshot)
    assert snap.player_id == "p:0"
    assert snap.position == (5, 3)
    assert snap.status == "idle"
    assert snap.goal == "no goal"
    assert snap.conversation_partner is None


def test_snapshot_for_pathfinding_player_shows_destination_as_goal():
    p = Player(id="p:0", position=Point(1, 1), facing=Vector(0, 1), last_input=0,
               pathfinding={"destination": Point(7, 8), "started": 0, "state": None})
    a = Agent(id="a:0", player_id="p:0")
    g = _make_game_with_players([p])
    g._agents = [a]
    g.world.agents = {a.id: a}
    snap = build_snapshot(g, "p:0")
    assert snap.status == "walking"
    assert "7" in snap.goal and "8" in snap.goal


def test_snapshot_for_active_player_shows_activity_as_goal():
    p = Player(id="p:0", position=Point(2, 2), facing=Vector(0, 1), last_input=0,
               activity=Activity(description="reading a book", emoji="book", until=99999))
    a = Agent(id="a:0", player_id="p:0")
    g = _make_game_with_players([p])
    g._agents = [a]
    g.world.agents = {a.id: a}
    snap = build_snapshot(g, "p:0")
    assert snap.status == "busy"
    assert "reading a book" in snap.goal


def test_snapshot_for_player_in_conversation_shows_partner_name():
    p1 = Player(id="p:0", position=Point(1, 1), facing=Vector(1, 0), last_input=0)
    p2 = Player(id="p:1", position=Point(2, 1), facing=Vector(-1, 0), last_input=0)
    from engine.conversation import Conversation, ConversationMembership
    from engine.state_machine import MembershipStatus
    conv = Conversation(id="c:0", creator="p:0", created=0,
                        participants={
                            "p:0": ConversationMembership("p:0", 0, MembershipStatus.participating(0)),
                            "p:1": ConversationMembership("p:1", 0, MembershipStatus.participating(0)),
                        })
    g = _make_game_with_players([p1, p2], conversations={"c:0": conv})
    from engine.player_description import PlayerDescription
    g.player_descriptions = {"p:1": PlayerDescription(player_id="p:1", character="f1", description="x", name="Bob")}
    snap = build_snapshot(g, "p:0")
    assert snap.status == "talking"
    assert snap.conversation_partner == "Bob"


def test_snapshot_for_unknown_player_returns_none():
    p = Player(id="p:0", position=Point(0, 0), facing=Vector(0, 0), last_input=0)
    g = _make_game_with_players([p])
    assert build_snapshot(g, "p:999") is None
