"""量化验证：网页地图的格子与游戏截图里的格子是否真的重合。

思路（不依赖肉眼看图）：
  后端每个 tile 带 left/top，是它左上角在**游戏截图**里的像素坐标。
  网页把整张截图贴到 world(first.x, first.y) 位置，公式见 route_map.html：

      img.x = world(first).x - tile/2 - first.left
      img.y = world(first).y - tile/2 - (first.top - 270)

  那么反推：截图里坐标 (px, py) 的点，在网页世界坐标里落在
      wx = px + img.x
      wy = py + img.y

  对任意一个 tile，它在截图里的中心是 (left + tile/2, top + tile/2)，
  贴到网页上应该正好等于 world(tile.x, tile.y)。
  如果两者差得多，就是没对齐。

这个脚本对每一对相邻 tile 计算误差，输出最大/中位误差。
"""
import sys
import json

sys.path.insert(0, r'C:\Users\黄尊鹏\OneDrive\文档\deepseek-harness\default-workspace\9wanmu')


def world(p, home, tile):
    return ((p[0] - home[0]) * tile, (home[1] - p[1]) * tile)


def check_frame(frame, home, tile):
    """返回这个帧里所有 tile 的 (dx, dy) 误差列表。

    注意：帧图是原图从 y=MAP_TOP(270) 处裁 880 高得到的，
    而 tile.left/top 是**原图**坐标。所以：
      帧图内坐标 = 原图坐标 - 270（仅 y 方向）
    前端公式 y = world.first.y - tile/2 - (first.top - 270) 已经体现了这点。
    """
    tiles = frame.get("tiles") or []
    if not tiles:
        return []
    first = tiles[0]
    MAP_TOP = 270
    # 网页贴图左上角（世界坐标）；图片内 y=0 对应原图 y=MAP_TOP
    fx, fy = world([first["x"], first["y"]], home, tile)
    img_x = fx - tile / 2 - first["left"]
    img_y = fy - tile / 2 - (first["top"] - MAP_TOP)

    errs = []
    for t in tiles:
        # 该 tile 在原图里的中心 → 帧图内坐标
        px = t["left"] + tile / 2
        py = (t["top"] - MAP_TOP) + tile / 2
        # 贴到网页后，这个中心的世界坐标
        wx = px + img_x
        wy = py + img_y
        # 应该落在哪
        ex, ey = world([t["x"], t["y"]], home, tile)
        errs.append((t["x"], t["y"], wx - ex, wy - ey))
    return errs


def report(state, label=""):
    home = state.get("home")
    if not home:
        print("%s没有 home，跳过" % label)
        return None
    # 后端在 view.tile 里给了真实格子边长；state.tileSize 可能没下发
    view = state.get("view") or {}
    tile = float(view.get("tile") or state.get("tileSize") or 67.5)
    print("%s后端 view.tile = %.2f | 前端 tileSize = %s"
          % (label, tile, state.get("tileSize")))
    frames = state.get("frames") or []
    if not frames:
        print("%s没有帧" % label)
        return None
    all_err = []
    for f in frames:
        all_err += check_frame(f, home, tile)
    if not all_err:
        print("%s没有 tile" % label)
        return None
    dxs = [abs(e[2]) for e in all_err]
    dys = [abs(e[3]) for e in all_err]
    mx = max(dxs)
    my = max(dys)
    medx = sorted(dxs)[len(dxs) // 2]
    medy = sorted(dys)[len(dys) // 2]
    print("  帧数=%d tile数=%d" % (len(frames), len(all_err)))
    print("  误差 dx: 最大 %.1f 中位 %.1f | dy: 最大 %.1f 中位 %.1f"
          % (mx, medx, my, medy))
    # 举几个最差的例子
    worst = sorted(all_err, key=lambda e: max(abs(e[2]), abs(e[3])), reverse=True)[:3]
    for x, y, dx, dy in worst:
        print("  最差 (%d,%d): dx=%+.1f dy=%+.1f" % (x, y, dx, dy))
    return max(mx, my)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        state = json.loads(open(sys.argv[1], encoding="utf-8").read())
    else:
        import urllib.request
        url = "http://127.0.0.1:8765/map/state"
        state = json.loads(urllib.request.urlopen(url, timeout=10).read().decode("utf-8"))
    worst = report(state)
    print()
    if worst is None:
        print("无法判定")
    elif worst <= 2:
        print("✅ 对齐（最大误差 %.1f px）" % worst)
    else:
        print("❌ 未对齐（最大误差 %.1f px）" % worst)
