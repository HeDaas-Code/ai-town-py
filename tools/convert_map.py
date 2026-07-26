"""把原项目的 data/gentle.js 地图文件转换为 JSON。

原文件以 ESM `export const` 形式导出 bgtiles / objmap / animatedsprites 等数组。
本脚本去掉 `export const` 前缀、计算 mapwidth/mapheight，然后在受限命名空间里
exec 出 Python 列表，最后落盘为 JSON 供游戏运行时加载。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def convert(js_path: Path, out_path: Path) -> None:
    src = js_path.read_text()

    # 0. 把 JS 行注释 `// ...` 转成 Python `# ...`，块注释 `/* */` 删除。
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    src = re.sub(r"//([^\n]*)", lambda m: "#" + m.group(1), src)

    # 1. 去掉所有 `export const` / `export var` 前缀，使其变为纯赋值。
    src = re.sub(r"export\s+const\s+", "", src)
    src = re.sub(r"export\s+var\s+", "", src)

    # 2. 去掉行尾分号（JS 风格），Python 不需要。
    src = src.replace(";", "")

    # 2b. JS 对象字面量简写 `{ x: 1, sheet: "a.json" }` 在 Python 中是非法的，
    #     需把键加上引号变成 `{"x": 1, "sheet": "a.json"}`。
    #     仅匹配出现在 `{` 或 `,` 之后的裸标识符键，避免误伤字符串内部。
    src = re.sub(r"([{,]\s*)([a-zA-Z_]\w*)\s*:", r'\1"\2":', src)

    # 2c. 删除末尾 `mapwidth = bgtiles[0].length` / `mapheight = ...` 这类
    #     依赖 JS 数组 `.length` 的语句，mapwidth/mapheight 由 Python 计算。
    src = re.sub(r"^\s*map(width|height)\s*=.*$", "", src, flags=re.MULTILINE)

    # 3. 把 `mapwidth = bgtiles[0].length;` 这类引用替换为占位赋值，
    #    稍后我们用 Python 计算真正的值。
    ns: dict = {}
    # 用 exec 执行数组定义（数组字面量在 JS 与 Python 中语法一致）。
    exec(src, ns)  # noqa: S102 - 受限转换工具

    bgtiles = ns["bgtiles"]
    objmap = ns["objmap"]
    animatedsprites = ns["animatedsprites"]

    mapwidth = len(bgtiles[0])
    mapheight = len(bgtiles[0][0])

    out = {
        "tilesetpath": "assets/gentle-obj.png",
        "tiledim": ns["tiledim"],
        "screenxtiles": ns["screenxtiles"],
        "screenytiles": ns["screenytiles"],
        "tilesetpxw": ns["tilesetpxw"],
        "tilesetpxh": ns["tilesetpxh"],
        "mapwidth": mapwidth,
        "mapheight": mapheight,
        "bgtiles": bgtiles,
        "objmap": objmap,
        "animatedsprites": animatedsprites,
    }
    out_path.write_text(json.dumps(out))
    print(f"converted {js_path.name}: {mapwidth}x{mapheight} -> {out_path}")


if __name__ == "__main__":
    js = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/gentle.js")
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("data/maps/gentle.json")
    convert(js, out)
