"""Read game state into a flat AgentSnapshot for TUI rendering.

Pure reads of game.world / game.player_descriptions / game.agent_descriptions.
No mutation. Safe to call from the TUI thread (GIL guards dict reads).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from engine.types import Point


@dataclass
class AgentSnapshot:
    player_id: str
    name: str
    position: Tuple[float, float]
    facing: Tuple[float, float]
    status: str            # "idle" | "walking" | "busy" | "talking"
    goal: str              # human-readable current objective
    conversation_partner: Optional[str]  # other player's name or None
    activity_description: Optional[str]
    plan: Optional[str]    # long-term plan from AgentDescription


def _player_name(game, player_id: str) -> str:
    pdesc = game.player_descriptions.get(player_id)
    if pdesc is not None:
        return pdesc.name
    return player_id


def _agent_for_player(game, player_id: str):
    for agent in game.world.agents.values():
        if agent.player_id == player_id:
            return agent
    return None


def build_snapshot(game, player_id: str) -> Optional[AgentSnapshot]:
    """Build a display snapshot for the given player. Returns None if not found."""
    player = game.world.players.get(player_id)
    if player is None:
        return None

    name = _player_name(game, player_id)
    pos = (player.position.x, player.position.y)
    facing = (player.facing.dx, player.facing.dy)

    # Determine conversation partner
    conv = game.world.player_conversation(player)
    partner_name: Optional[str] = None
    if conv is not None:
        for pid in conv.participants.keys():
            if pid != player_id:
                partner_name = _player_name(game, pid)
                break

    activity_desc = player.activity.description if player.activity is not None else None

    # Derive status and goal with priority: talking > walking > busy > idle
    if conv is not None:
        status = "talking"
        goal = f"talking with {partner_name}" if partner_name else "talking"
    elif player.pathfinding is not None:
        dest = player.pathfinding.get("destination")
        status = "walking"
        if dest is not None:
            goal = f"heading to ({dest.x:.0f}, {dest.y:.0f})"
        else:
            goal = "walking"
    elif activity_desc is not None:
        status = "busy"
        goal = activity_desc
    else:
        status = "idle"
        goal = "no goal"

    # Long-term plan from AgentDescription
    plan: Optional[str] = None
    agent = _agent_for_player(game, player_id)
    if agent is not None:
        adesc = game.agent_descriptions.get(agent.id)
        if adesc is not None:
            plan = getattr(adesc, "plan", None)

    return AgentSnapshot(
        player_id=player_id,
        name=name,
        position=pos,
        facing=facing,
        status=status,
        goal=goal,
        conversation_partner=partner_name,
        activity_description=activity_desc,
        plan=plan,
    )
