"""Map WorldMap tile IDs to ASCII characters for the TUI map view.

Index convention (per engine/world_map.py): bg_tiles[layer][x][y], where
-1 means empty. Unknown tile IDs fall back to grass ('.') so the map is
always readable even before the mapping is fully populated.

Population notes (from inspecting data/maps/gentle.json):
- ~250 distinct bg tile IDs; the engine has no terrain classification
  (only object tiles carry collision). Without tileset pixel analysis we
  cannot reliably distinguish grass/water/mountain by ID alone, so all bg
  tiles use the grass default ('.').
- 2 distinct object tile IDs (367, 458) represent buildings/props and are
  mapped to '□' so they render distinctly on top of the grass field.
"""
from __future__ import annotations

# Configurable mapping: tile_id -> char.
TILE_CHARS: dict[int, str] = {
    # Object tiles (buildings/props) — render distinctly from grass.
    367: "\u25a1",  # □
    458: "\u25a1",  # □
}

# Characters used for agents overlaid on the map.
AGENT_CHAR = "@"
OTHER_AGENT_CHAR = "o"

# Fallback char for unknown (non-empty) tiles.
_DEFAULT_CHAR = "."


def tile_to_char(tile_id: int) -> str:
    """Return the ASCII char for a tile ID.

    -1 (empty) -> space; unknown positive IDs -> grass '.'; known -> mapped char.
    """
    if tile_id is None or tile_id < 0:
        return " "
    return TILE_CHARS.get(tile_id, _DEFAULT_CHAR)
