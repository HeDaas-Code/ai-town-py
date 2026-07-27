"""Map WorldMap tile IDs to ASCII characters for the TUI map view.

Index convention (per engine/world_map.py): bg_tiles[layer][x][y], where
-1 means empty. Unknown tile IDs fall back to grass ('.') so the map is
always readable even before the mapping is fully populated.

Inspect the actual map to populate TILE_CHARS:
    python -c "import json; d=json.load(open('data/maps/gentle.json')); \
        print(sorted(set(t for layer in d['bgtiles'] for col in layer for t in col if t>=0)))"
"""
from __future__ import annotations

# Configurable mapping: tile_id -> char. Populate after inspecting the map.
TILE_CHARS: dict[int, str] = {
    # Defaults intentionally sparse; refine after running inspect_tiles().
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
