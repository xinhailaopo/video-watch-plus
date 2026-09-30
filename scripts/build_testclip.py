#!/usr/bin/env python3
"""build_testclip.py —— 造一段**带已知基准真值**的验收素材。

为什么需要它
------------
你无法用一个"不知道正确答案"的素材去验证一条分析链。本脚本用 ffmpeg 的合成源
造一段时间轴完全已知的短片，用来验收 video-watch 的量测层与 audio_probe 的声学层。

它同时是一份**反例集**：脚本里保留了两个已经踩过的坑（见文件末尾注释），
因为把失败方案写在旁边，比只写成功方案有用。

造出来的素材（默认 9 秒 / 640x360 / 24fps）
------------------------------------------
  0.0–3.0s  纯暗红 + 文字 SEG-1 IDLE     静止
  3.0–6.0s  testsrc2 运动画面 + SEG-2 PLAY 高运动
  6.0–9.0s  纯暗蓝 + SEG-3 STATIC         静止
  音频      3.66s 静音 → 之后 440Hz 正弦

对应的基准真值
--------------
  硬切点        3.0 / 6.0（片尾 9.0 是文件结束，不是切点）
  冻结段起点    0.0 / 6.0
  静音段        起始 0.0，结束 3.66
  内容判型      含硬切的剪辑类

实测结果见 verification/ 下的记录：切点、冻结、静音三项全部精确命中。

用法
----
    python build_testclip.py out/                 # 生成 out/clip.mp4
    python build_testclip.py out/ --seconds 9 --size 1280x720 --fps 30
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from audio_probe import _config_ffmpeg  # 复用同一份配置查找逻辑
except ImportError:  # pragma: no cover
    def _config_ffmpeg(_=None):
        return None


def find_ffmpeg(explicit: str | None) -> str:
    for c in (explicit, os.environ.get("VW_FFMPEG"), _config_ffmpeg(None), shutil.which("ffmpeg")):
        if c and os.path.exists(c):
            return c
    sys.exit("找不到 ffmpeg（用 --ffmpeg、VW_FFMPEG、vw.config.json 或 PATH 指定）")


def run(args: list[str], tag: str) -> None:
    p = subprocess.run(args, capture_output=True, text=True, errors="replace")
    if p.returncode != 0:
        print(f"[FAIL] {tag}\n{(p.stderr or '').strip()[-700:]}", file=sys.stderr)
        sys.exit(1)
    print(f"[ok]   {tag}")


def main() -> None:
    ap = argparse.ArgumentParser(description="生成带已知真值的验收素材")
    ap.add_argument("outdir")
    ap.add_argument("--seconds", type=float, default=9.0, help="总时长（默认 9，三等分）")
    ap.add_argument("--size", default="640x360", help="分辨率（默认 640x360）")
    ap.add_argument("--fps", type=int, default=24)
    ap.add_argument("--ffmpeg", default=None)
    ap.add_argument("--font", default="consola.ttf",
                    help="drawtext 用的字体文件（相对 outdir 或绝对路径）")
    args = ap.parse_args()

    ff = find_ffmpeg(args.ffmpeg)
    out = os.path.abspath(args.outdir)
    os.makedirs(out, exist_ok=True)
    os.chdir(out)

    third = args.seconds / 3.0
    silence = round(third + 0.66, 2)          # 静音比首段略长，便于检验边界
    tone = round(args.seconds - silence, 2)

    # drawtext 的字体路径：在 Windows 上必须走"相对路径 + 工作目录"最稳，
    # 绝对盘符路径要整体加单引号才被 ffmpeg 接受（见 patches/vw-windows-fixes.patch）。
    font = args.font
    if not os.path.exists(font):
        for c in (os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", font),
                  "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
            if os.path.exists(c):
                shutil.copy(c, os.path.join(out, os.path.basename(c)))
                font = os.path.basename(c)
                break
    if not os.path.exists(font):
        print(f"提示：找不到字体 {args.font}，图版将不带烧入索引", file=sys.stderr)

    def dt(text: str) -> str:
        return (f"drawtext=fontfile={font}:text='{text}':fontsize=44:"
                "fontcolor=white:x=(w-tw)/2:y=(h-th)/2")

    # 运动段用 testsrc2：天然逐帧变化，不依赖任何表达式型滤镜。
    graph = (f"[0:v]{dt('SEG-1 IDLE')}[v0];"
             f"[1:v]{dt('SEG-2 PLAY')}[v1];"
             f"[2:v]{dt('SEG-3 STATIC')}[v2];"
             "[v0][v1][v2]concat=n=3:v=1:a=0[out]")

    w, h = args.size.split("x")
    print(f"== 1/3 视频：3 段，切点 {third:.1f} / {third*2:.1f} / {args.seconds:.1f}s ==")
    run([ff, "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"color=c=0x3a1010:s={args.size}:r={args.fps}:d={third}",
         "-f", "lavfi", "-i", f"testsrc2=s={args.size}:r={args.fps}:d={third}",
         "-f", "lavfi", "-i", f"color=c=0x10103a:s={args.size}:r={args.fps}:d={third}",
         "-filter_complex", graph, "-map", "[out]",
         "-pix_fmt", "yuv420p", "-c:v", "libx264", "-crf", "20", "seg.mp4"], "seg.mp4")

    print(f"== 2/3 音频：{silence}s 静音 → {tone}s 440Hz ==")
    run([ff, "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono:d={silence}",
         "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={tone}",
         "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[a]", "-map", "[a]",
         "-c:a", "aac", "-b:a", "96k", "tone.m4a"], "tone.m4a")

    print("== 3/3 封装 ==")
    run([ff, "-y", "-hide_banner", "-loglevel", "error", "-i", "seg.mp4", "-i", "tone.m4a",
         "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "copy", "clip.mp4"], "clip.mp4")

    truth = {
        "clip": "clip.mp4", "seconds": args.seconds, "size": args.size, "fps": args.fps,
        "ground_truth": {
            "cuts_s": [round(third, 2), round(third * 2, 2)],
            "freeze_starts_s": [0.0, round(third * 2, 2)],
            "silence_start_s": 0.0, "silence_end_s": silence,
            "content_kind": "剪辑类（含硬切）",
            "audio_kind": "前段静音 + 后段单音（无人声）",
        },
        "expected_probe": {
            "cuts": [round(third, 2), round(third * 2, 2)],
            "freezes_first_two": [0.0, round(third * 2, 2)],
            "silences": [{"kind": "start", "t": 0.0}, {"kind": "end", "t": silence}],
        },
        "expected_audio_probe_verdict": "不应判为含人声（本素材是纯单音与静音）",
    }
    with open("ground_truth.json", "w", encoding="utf-8") as fh:
        json.dump(truth, fh, ensure_ascii=False, indent=1)
    print("\n→ clip.mp4 + ground_truth.json")
    print(json.dumps(truth["ground_truth"], ensure_ascii=False, indent=1))


# --------------------------------------------------------------------------- #
# 两个已经付过代价的坑（保留在此，避免后来者重走）
#
# 1) drawbox 放在 concat **之前**时，它的时间基是"段内 0..N 秒"而不是全片时间。
#    于是 x='(t-3)*140' 在整段里恒为负 -> 方块被画到画外，视频里根本看不到运动。
#    要造运动段，直接用 testsrc2 这类天然运动源最省事。
#
# 2) 本机 ffmpeg N-126965 上，drawbox 的**表达式型 x 不产出图形**
#    （x=100 有效、x='(t-3)*140' 无效，signalstats 对照可验证）。
#    需要动态图形时改用 overlay。
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    main()
