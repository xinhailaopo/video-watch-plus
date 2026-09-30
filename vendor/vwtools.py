"""vwtools —— 逐帧取证的可复用管道。

**只管管道，不管语义。** 解帧、掩码、连通域、逐帧追踪、差分取证、可视化这些
对任何视频都成立；"什么颜色是恐龙""哪个框里该有几只"是每个视频现写的领域知识，
写在各自的专用脚本里，不进这个模块。

典型用法（每个视频一份专用脚本，通常十几行）：

    from vwtools import frames, mask_by, components, track_box, diff_lost

    fr = frames(media)
    dino = lambda a: (a[..., 2] > 150) & (a[..., 1] - a[..., 0] > 15)   # 本片的恐龙判据
    for m in components(dino(fr[0])):
        print(m)
"""
import json
import os
import shutil
import subprocess
import tempfile
from collections import deque

import numpy as np
from PIL import Image, ImageSequence


def _resolve_ffmpeg():
    """ffmpeg 路径：VW_FFMPEG > VW_CONFIG 指定的配置 > 默认位置的配置 > PATH。"""
    if os.environ.get("VW_FFMPEG"):
        return os.environ["VW_FFMPEG"]
    for path in (os.environ.get("VW_CONFIG"),
                 os.path.join(os.getcwd(), "vw.config.json"),
                 os.path.join(os.path.expanduser("~"), ".config", "video-watch",
                              "vw.config.json")):
        if not path:
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                found = json.load(fh).get("ffmpeg")
        except (OSError, ValueError):
            continue
        if found:
            return found
    return shutil.which("ffmpeg") or "ffmpeg"


FFMPEG = _resolve_ffmpeg()


def frames(media, limit=None, size=None):
    """解出全部帧为 RGB ndarray 列表（GIF/WebP 用 Pillow，视频用 ffmpeg）。"""
    ext = os.path.splitext(media)[1].lower()
    if ext in (".gif", ".webp"):
        im = Image.open(media)
        out = []
        for page in ImageSequence.Iterator(im):
            a = np.array(page.convert("RGB"))
            if size and (a.shape[1], a.shape[0]) != size:
                a = np.array(Image.fromarray(a).resize(size, Image.LANCZOS))
            out.append(a)
            if limit and len(out) >= limit:
                break
        return out

    with tempfile.TemporaryDirectory() as td:
        vf = f"scale={size[0]}:{size[1]}" if size else "null"
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-i", media,
                        "-vf", vf, "-vsync", "0",
                        os.path.join(td, "f%06d.png")], check=True)
        files = sorted(os.listdir(td))
        if limit:
            files = files[:limit]
        return [np.array(Image.open(os.path.join(td, f)).convert("RGB")) for f in files]


def mask_by(rule, frame):
    """按专用脚本给的判据生成布尔掩码；rule 接收 RGB ndarray，返回布尔 ndarray。"""
    return rule(frame)


def components(mask, min_px=400, step=4, gap=1):
    """连通域：返回 [{px, box}]，box = (x, y, w, h)。step 为粗化步长（加速）。"""
    ys, xs = np.nonzero(mask)
    if len(ys) == 0:
        return []
    grid = {}
    for y, x in zip(ys // step, xs // step):
        grid[(y, x)] = grid.get((y, x), 0) + 1

    seen, comps = set(), []
    for cell in list(grid):
        if cell in seen:
            continue
        q, cells = deque([cell]), []
        seen.add(cell)
        while q:
            cy, cx = q.popleft()
            cells.append((cy, cx))
            for dy in range(-gap, gap + 1):
                for dx in range(-gap, gap + 1):
                    n = (cy + dy, cx + dx)
                    if n in grid and n not in seen:
                        seen.add(n)
                        q.append(n)
        px = sum(grid[c] for c in cells)
        if px < min_px:
            continue
        cy = [c[0] for c in cells]
        cx = [c[1] for c in cells]
        comps.append({"px": px, "box": (min(cx) * step, min(cy) * step,
                                        (max(cx) - min(cx) + 1) * step,
                                        (max(cy) - min(cy) + 1) * step)})
    return sorted(comps, key=lambda c: -c["px"])


def track_box(frames_list, box, rule, fps=None, jump=300, label=""):
    """盯住一个框逐帧计数，标出突变帧——"什么时候少了一个"就这么找。"""
    x, y, w, h = box
    rows, prev = [], None
    for i, f in enumerate(frames_list):
        n = int(rule(f)[y:y + h, x:x + w].sum())
        delta = None if prev is None else n - prev
        rows.append({"i": i, "t": (i / fps) if fps else None, "n": n, "delta": delta})
        if delta is not None and abs(delta) >= jump:
            print(f"  {label}帧{i} t={rows[-1]['t']:.2f}s 突变 {delta:+d} → {n}")
        prev = n
    return rows


def diff_lost(frames_list, ia, ib, rule, out_png=None, min_px=400):
    """对比两帧，找出"丢失的"与"新增的"掩码区域，并可选叠加到原帧上。

    这是找"极快事件"的主力：0.1 秒级的甩飞在采样里看不见，但差分一帧不漏。
    """
    ma, mb = rule(frames_list[ia]), rule(frames_list[ib])
    lost, gained = ma & ~mb, ~ma & mb
    res = {"lost": components(lost, min_px), "gained": components(gained, min_px),
           "lost_px": int(lost.sum()), "gained_px": int(gained.sum())}
    if out_png:
        ov = frames_list[ia].copy()
        ov[lost] = [255, 40, 40]
        ov[gained] = [40, 255, 40]
        Image.fromarray(ov).save(out_png)
    return res


def report(res, tag=""):
    print(f"{tag}丢失 {res['lost_px']} 像素，新增 {res['gained_px']} 像素")
    for kind in ("lost", "gained"):
        for c in res[kind][:8]:
            print(f"  {kind}: 面积 {c['px']:>6}  框 x,y,w,h = {c['box']}")
