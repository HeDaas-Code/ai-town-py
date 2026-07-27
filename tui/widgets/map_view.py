"""Center column: ASCII俯视 map centered on the selected agent."""
from __future__ import annotations

from textual.widgets import Static

from tui.tile_chars import tile_to_char, AGENT_CHAR, OTHER_AGENT_CHAR


class MapView(Static):
    """Renders a viewport of the world map as ASCII.

    The app calls ``update_view`` each refresh with:
    - ``grid``: list of strings (one per row) of pre-rendered map chars
    - ``center_name``: name of the centered agent (for the header)
    """

    def render_map(self, world_map, center_x: float, center_y: float,
                   view_w: int, view_h: int, players, selected_player_id: str) -> list:
        """Build the ASCII grid. Returns a list of strings (rows).

        ``players`` is a dict of player_id -> Player. ``selected_player_id``
        is the player to mark with '@' at the center.
        """
        half_w = view_w // 2
        half_h = view_h // 2
        # Build a position lookup for quick overlay
        # player positions are floats; we mark the tile they occupy
        overlay = {}
        for pid, p in players.items():
            tx = int(p.position.x)
            ty = int(p.position.y)
            overlay[(tx, ty)] = pid

        rows = []
        for dy in range(-half_h, half_h + 1):
            row_chars = []
            for dx in range(-half_w, half_w + 1):
                wx = int(center_x) + dx
                wy = int(center_y) + dy
                if (wx, wy) in overlay:
                    pid = overlay[(wx, wy)]
                    if pid == selected_player_id:
                        row_chars.append(AGENT_CHAR)
                    else:
                        row_chars.append(OTHER_AGENT_CHAR)
                else:
                    row_chars.append(self._read_tile(world_map, wx, wy))
            rows.append("".join(row_chars))
        return rows

    def _read_tile(self, world_map, x: int, y: int) -> str:
        if x < 0 or y < 0 or x >= world_map.width or y >= world_map.height:
            return " "
        # object layer first (buildings sit on top of grass)
        char = " "
        for layer in world_map.object_tiles:
            if x < len(layer) and y < len(layer[x]):
                t = layer[x][y]
                if t is not None and t >= 0:
                    char = tile_to_char(t)
                    break
        if char == " ":
            for layer in world_map.bg_tiles:
                if x < len(layer) and y < len(layer[x]):
                    t = layer[x][y]
                    if t is not None and t >= 0:
                        char = tile_to_char(t)
                        break
        return char

    def update_view(self, grid, center_name: str) -> None:
        legend = "@=selected  o=other  .=grass  #=terrain  space=empty"
        header = f" {center_name}  |  {legend}"
        body = "\n".join(grid)
        self.update(f"{header}\n{body}")
