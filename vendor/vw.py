#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# 本文件来自上游 video-watch-skill（MIT，Copyright (c) 2026 CFITSec）
#   https://github.com/CFITCorporation/video-watch-skill
# 本仓库对其做了两处 Windows 兼容性修补，完整差异见
#   patches/vw-windows-fixes.patch
#   1) escape_filter_path()：盘符路径必须整体加单引号，否则 drawtext 报错、grid/seq/read 全挂
#   2) magick()：IM 7 的 `magick convert` 入口在便携构建下不被识别，须退化为裸入口
# 除这两处外，本文件与上游 1.1 一致。上游版权声明见 vendor/LICENSE.video-watch。
# ---------------------------------------------------------------------------
"""video-watch：把视频/动图翻译成可核算的图版序列。

probe  量测层。ffmpeg 逐帧取切点、冻结、运动量、静音段，产出 timeline.json。
plan   采样层。骨架优先的抽样计划，保证覆盖盲区有上界，产出 shotlist.json。
sheet  图版层。按计划渲染联络表与位置映射清单（第几行第几列 = 第几秒）。
gif    动图线。GIF/WebP 按帧延迟归一化成真实时间轴，再走同一条路。

视觉预算：一张 756x756 图计费约 346 token，买 324 个网格格。
拼版换覆盖率，裁切放大换分辨率，两者不可兼得。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

# drawtext 需要 ffmpeg 编译时带 libfreetype；缺了它 sheet/grid/seq 烧不上索引，
# 而报错落在抽帧那一步，离真正原因很远，所以并入启动校验。
REQUIRED_FILTERS = ("scdet", "freezedetect", "silencedetect", "tblend", "signalstats",
                    "tile", "drawtext")
BUILTIN_FONTS = [
    "C:/Windows/Fonts/consola.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/msyh.ttc",
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
]
CONFIG_NAME = "vw.config.json"
SHEET_PX = 756
TOKENS_PER_IMAGE = 346
COLS_FOR = {9: 3, 16: 4, 25: 5, 36: 6}
IDLE_MOTION = 0.05
MIN_GAP = 0.6
PTS_RE = re.compile(r"pts_time:([0-9.]+)")


def die(msg):
    print(f"错误：{msg}", file=sys.stderr)
    sys.exit(2)


CONFIG = {}
CONFIG_OVERRIDE = None
CONFIG_PATH = None


def config_candidates():
    """配置文件查找顺序：--config > VW_CONFIG > 当前目录 > 用户目录。"""
    paths = []
    if CONFIG_OVERRIDE:
        paths.append(CONFIG_OVERRIDE)
    if os.environ.get("VW_CONFIG"):
        paths.append(os.environ["VW_CONFIG"])
    paths.append(os.path.join(os.getcwd(), CONFIG_NAME))
    paths.append(os.path.join(os.path.expanduser("~"), ".config", "video-watch", CONFIG_NAME))
    return paths


def load_config(override=None):
    """载入第一个存在的配置文件；缺失的键由内置默认兜底。"""
    global CONFIG, CONFIG_OVERRIDE, CONFIG_PATH
    CONFIG_OVERRIDE = override
    for path in config_candidates():
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                CONFIG = json.load(fh) or {}
        except (OSError, ValueError) as exc:
            die(f"配置文件 {path} 读不了：{exc}")
        CONFIG_PATH = path
        return CONFIG
    CONFIG, CONFIG_PATH = {}, None
    return CONFIG


def cfg(key, default):
    """配置 defaults 段里的默认参数值。"""
    return (CONFIG.get("defaults") or {}).get(key, default)


def ffmpeg_candidates():
    """ffmpeg 候选：VW_FFMPEG > 配置 ffmpeg > PATH。"""
    return [c for c in (os.environ.get("VW_FFMPEG"), CONFIG.get("ffmpeg"),
                        shutil.which("ffmpeg")) if c]


def font_candidates():
    """字体候选：VW_FONT > 配置 font > 各平台内置路径。"""
    return [c for c in (os.environ.get("VW_FONT"), CONFIG.get("font"),
                        *BUILTIN_FONTS) if c]


def out_dir(args, name, fallback=None):
    """输出目录：命令行 --out > 配置 outdir 根 + name > 内置默认。"""
    if args.out:
        return os.path.abspath(args.out)
    root = CONFIG.get("outdir")
    if root:
        return os.path.abspath(os.path.join(root, name) if name else root)
    return os.path.abspath(fallback if fallback is not None else name)


def scan_ffmpeg():
    """逐个候选探测，返回 [(候选, 状态, 缺失滤镜)]，状态为 ok/missing/path/exec。"""
    rows = []
    for cand in ffmpeg_candidates():
        if not cand:
            continue
        if os.path.sep in cand and not os.path.exists(cand):
            rows.append((cand, "path", []))
            continue
        try:
            out = subprocess.run([cand, "-hide_banner", "-filters"],
                                 capture_output=True, text=True, timeout=30).stdout
        except OSError:
            rows.append((cand, "exec", []))
            continue
        missing = [f for f in REQUIRED_FILTERS if not re.search(rf"\s{f}\s", out)]
        rows.append((cand, "ok" if not missing else "missing", missing))
    return rows


def resolve_ffmpeg():
    for cand, state, missing in scan_ffmpeg():
        if state == "ok":
            return cand
        if state == "missing":
            die(f"{cand} 缺少滤镜 {', '.join(missing)}；"
                f"PATH 里的 ffmpeg 可能是裁剪版，请设置 VW_FFMPEG 指向完整版")
    die("找不到可用的 ffmpeg（需要含 scdet/freezedetect/drawtext 的完整构建，"
        "可用 VW_FFMPEG 指定路径）")


def run(cmd, cwd=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", cwd=cwd)
    except OSError as exc:
        die(f"无法执行 {cmd[0]}：{exc}")
    if r.returncode != 0:
        die(f"命令失败：{cmd[0]} …\n{r.stderr.strip()[:500]}")
    return r


def _version_line(args):
    """跑一条取版本的命令并返回首行；执行不了（没装）返回 None。"""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except OSError:
        return None
    text = ((r.stdout or "") + (r.stderr or "")).strip()
    return text.splitlines()[0] if text else ""


def _im_major(exe):
    line = _version_line([exe, "-version"])
    if not line or "ImageMagick" not in line:
        return 0
    m = re.search(r"ImageMagick (\d+)", line)
    return int(m.group(1)) if m else 0


_MAGICK = None


def find_magick():
    """探测 ImageMagick，返回 (主版本号, 可执行文件)，没有则 None。

    7.x 有 magick 聚合入口；6.x 的 montage/convert/identify 是三个独立命令，
    且 Windows 上的 convert 会撞系统自带的 convert.exe，所以按版本号验。
    """
    for cand in (os.environ.get("VW_MAGICK"), CONFIG.get("magick")):
        if cand:
            major = _im_major(cand)
            if major:
                return (major, cand)
    exe = shutil.which("magick")
    if exe and _im_major(exe) >= 7:
        return (7, exe)
    if all(shutil.which(c) for c in ("montage", "convert", "identify")):
        return (6, "")
    return None


def resolve_magick():
    global _MAGICK
    if _MAGICK is None:
        _MAGICK = find_magick()
    if _MAGICK is None:
        die("找不到 ImageMagick（拼贴图版与差分取证需要它）：装 ImageMagick 7，"
            "或用 VW_MAGICK 指向 magick 可执行文件")
    return _MAGICK


def magick(sub, *args):
    """拼出 ImageMagick 命令：7.x 走 magick 聚合入口，6.x 走独立命令。

    本机实测（ImageMagick 7.1.2-32 Q16 x64 portable / win64）：
      magick convert a b -append out  -> 失败：no decode delegate for `convert'
                                          （convert 未被识别为子命令，被当成输入文件名）
      magick montage ...              -> 成功
      magick a b -append out          -> 成功（IM7 原生写法，convert 的功能由裸入口承担）
    montage / identify / compare / composite / mogrify / stream / conjure 均被识别，
    唯独 convert 不是，故 7.x 下 convert 必须退化为裸入口，否则 seq / sheet / --diff 全部报错。
    """
    major, exe = resolve_magick()
    if major >= 7:
        prefix = [exe] if sub == "convert" else [exe, sub]
    else:
        prefix = [shutil.which(sub) or sub]
    return run([*prefix, *args])


def escape_filter_path(path):
    """ffmpeg filter 参数里冒号是分隔符：Windows 盘符要转义，反斜杠统一成正斜杠。

    本机实测（ffmpeg N-126965 win64-gpl, drawtext）：
      fontfile=C:/x.ttf        -> 失败
      fontfile=C\\:/x.ttf      -> 失败   （仅转义冒号，ffmpeg 把 \\: 当分隔符吃掉）
      fontfile='C\\:/x.ttf'    -> 成功   （转义冒号 + 整体加单引号）
      fontfile=C\\\\:/x.ttf    -> 成功
    因此 Windows 上必须给路径整体加单引号，否则任何带盘符的字体都读不到。
    """
    p = path.replace("\\", "/").replace(":", "\\:")
    if os.name == "nt":
        return "'" + p + "'"
    return p


def find_font():
    """返回原始字体路径（未做 ffmpeg 转义），供写配置与 doctor 显示。"""
    for cand in font_candidates():
        if cand and os.path.exists(cand):
            return cand
    return None


def resolve_font():
    """返回可直接放进 ffmpeg filter 的字体路径。"""
    font = find_font()
    if font is None:
        die("找不到可用于 drawtext 的字体：装 DejaVu 或 Noto 字体，"
            "或用 VW_FONT 指向一个 .ttf/.ttc")
    return escape_filter_path(font)


def find_ffprobe(ffmpeg):
    """ffprobe 通常与 ffmpeg 同目录但未必带 .exe 后缀，两个名字都试。"""
    if ffmpeg:
        d = os.path.dirname(os.path.abspath(ffmpeg))
        for name in ("ffprobe", "ffprobe.exe"):
            cand = os.path.join(d, name)
            if os.path.exists(cand):
                return cand
    for cand in (os.environ.get("VW_FFPROBE"), CONFIG.get("ffprobe")):
        if cand:
            return cand
    return shutil.which("ffprobe")


def resolve_ffprobe(ffmpeg):
    probe = find_ffprobe(ffmpeg)
    if probe is None:
        die("找不到 ffprobe（通常与 ffmpeg 一起安装，可用 VW_FFPROBE 指定）")
    return probe


def ffprobe_info(ffmpeg, media):
    probe = resolve_ffprobe(ffmpeg)
    out = run([probe, "-v", "error", "-print_format", "json",
               "-show_format", "-show_streams", media]).stdout
    info = json.loads(out)
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    if video is None:
        die("该文件没有视频流")
    audio = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    num, _, den = video.get("r_frame_rate", "0/1").partition("/")
    fps = float(num) / float(den) if float(den or 0) else 0.0
    return {
        "duration": float(info["format"].get("duration") or 0.0),
        "size": int(info["format"].get("size") or 0),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "fps": round(fps, 3),
        "nb_frames": int(video.get("nb_frames") or 0),
        "video_codec": video.get("codec_name"),
        "audio_codec": (audio or {}).get("codec_name"),
        "audio_channels": (audio or {}).get("channels"),
    }


def parse_metadata(path):
    recs, cur = [], None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if line.startswith("frame:"):
                if cur is not None:
                    recs.append(cur)
                m = PTS_RE.search(line)
                cur = {"t": float(m.group(1)) if m else None}
            elif cur is not None and line.startswith("lavfi."):
                key, _, raw = line.partition("=")
                try:
                    cur[key] = float(raw)
                except ValueError:
                    cur[key] = raw
    if cur is not None:
        recs.append(cur)
    return recs


def parse_silence(stderr):
    events = []
    for line in stderr.splitlines():
        m = re.search(r"silence_(start|end): ([0-9.]+)", line)
        if m:
            events.append({"kind": m.group(1), "t": round(float(m.group(2)), 2)})
    return events


def classify(dur, cuts, motion_mean, p95, active_ratio):
    per_min = cuts / (dur / 60) if dur else 0.0
    if per_min >= 4:
        return ("剪辑类（影视/演示/短视频）",
                "切点密集，可作主信号；运动曲线作补充")
    if motion_mean < 1.0 and active_ratio < 0.35:
        return ("低运动屏幕录制（阅读/文档/静态界面）",
                "切点稀疏甚至为零，主信号是运动曲线的活跃段，不是切点")
    if p95 >= 8:
        return ("高运动连续画面（游戏/动画/体育）",
                "切点偏少而运动量大，主信号是运动尖峰")
    return ("一般连续画面", "切点与运动曲线并用")


def cmd_probe(args):
    ffmpeg = resolve_ffmpeg()
    media = os.path.abspath(args.media)
    if not os.path.exists(media):
        die(f"找不到文件 {media}")
    outdir = out_dir(args, "vw_out")
    os.makedirs(outdir, exist_ok=True)

    info = ffprobe_info(ffmpeg, media)
    print(f"素材 {os.path.basename(media)}  {info['width']}x{info['height']} "
          f"{info['fps']}fps  {info['duration']:.2f}s  "
          f"视频 {info['video_codec']}  音频 {info['audio_codec'] or '无'}")

    run([ffmpeg, "-hide_banner", "-nostats", "-i", media,
         "-vf", (f"scdet=t={args.scdet},freezedetect=n=-60dB:d=2,"
                 "tblend=all_mode=difference,signalstats,"
                 "metadata=mode=print:file=analysis_v.txt"),
         "-an", "-f", "null", "-"], cwd=outdir)

    audio_events = []
    if info["audio_codec"] and not args.no_audio:
        r = subprocess.run([ffmpeg, "-hide_banner", "-nostats", "-i", media, "-vn",
                            "-af", "silencedetect=n=-35dB:d=1", "-f", "null", "-"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", cwd=outdir)
        audio_events = parse_silence(r.stderr)

    recs = parse_metadata(os.path.join(outdir, "analysis_v.txt"))
    if not recs:
        die("量测输出为空，ffmpeg 可能未正常产出元数据")

    buckets = {}
    for r in recs:
        if r["t"] is None:
            continue
        b = buckets.setdefault(int(r["t"]), {"n": 0, "sum": 0.0, "motion_max": 0.0,
                                             "pix_max": 0.0, "pix_high": 0.0,
                                             "score_max": 0.0, "score_sum": 0.0})
        b["n"] += 1
        m = r.get("lavfi.signalstats.YAVG")
        if isinstance(m, float):
            b["sum"] += m
            b["motion_max"] = max(b["motion_max"], m)
        ymax = r.get("lavfi.signalstats.YMAX")
        if isinstance(ymax, float):
            b["pix_max"] = max(b["pix_max"], ymax)
        yhigh = r.get("lavfi.signalstats.YHIGH")
        if isinstance(yhigh, float):
            b["pix_high"] = max(b["pix_high"], yhigh)
        s = r.get("lavfi.scd.score")
        if isinstance(s, float):
            b["score_max"] = max(b["score_max"], s)
            b["score_sum"] += s

    motion = {s: buckets[s]["sum"] / buckets[s]["n"] for s in buckets}
    secs = sorted(buckets)
    vals = sorted(motion.values())
    dur = recs[-1]["t"]
    p95 = vals[int(len(vals) * 0.95)] if vals else 0.0
    active_ratio = sum(1 for s in secs if motion[s] >= IDLE_MOTION) / len(secs) if secs else 0.0
    cuts = sorted(round(r["t"], 2) for r in recs
                  if isinstance(r.get("lavfi.scd.score"), float)
                  and r["lavfi.scd.score"] >= args.scdet)
    freezes = []
    for r in recs:
        start = r.get("lavfi.freezedetect.freeze_start")
        if isinstance(start, float):
            freezes.append(round(start, 2))

    kind, advice = classify(dur, len(cuts), sum(vals) / len(vals) if vals else 0.0,
                            p95, active_ratio)
    # tblend 是双输入滤镜，首帧没有前帧可混合因而不产出，元数据条数比真实帧数少 1
    n_frames = info.get("nb_frames") or len(recs) + 1
    timeline = {
        "media": os.path.basename(media),
        "info": info,
        "content_kind": kind,
        "advice": advice,
        "duration": round(dur, 3),
        "frames": n_frames,
        "cuts": cuts,
        "freezes": freezes,
        "silences": audio_events,
        "motion_p95": round(p95, 3),
        "active_ratio": round(active_ratio, 3),
        "buckets": {str(s): {"motion": round(motion[s], 4),
                             "motion_max": round(buckets[s]["motion_max"], 4),
                             "pix_max": round(buckets[s]["pix_max"], 1),
                             "pix_high": round(buckets[s]["pix_high"], 1),
                             "score_max": round(buckets[s]["score_max"], 2)}
                    for s in secs},
    }
    path = os.path.join(outdir, "timeline.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(timeline, fh, ensure_ascii=False)

    # 帧率刻意用记录数算：(N-1) / ((N-1)/fps) = fps；换成 n_frames 会偏大，别改
    print(f"帧 {n_frames}  实际帧率 {len(recs) / dur:.2f}")
    print(f"内容判型 {kind}")
    print(f"  工具口径提示：{advice}")
    print(f"切点 {len(cuts)} 个  冻结段 {len(freezes)} 个  静音事件 {len(audio_events)} 个")
    print(f"运动量 均值 {timeline and (sum(vals) / len(vals)):.3f}  p95 {p95:.3f}  "
          f"max {vals[-1] if vals else 0:.3f}  活跃秒占比 {active_ratio:.0%}")
    if not cuts:
        print("  ! 本素材无硬切点：采样必须由运动曲线与骨架承担")
    print(f"→ {path}")


def load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def segments(secs, motion, idle=IDLE_MOTION):
    out, start = [], secs[0]
    state = "idle" if motion[start] < idle else "active"
    for i in range(1, len(secs)):
        st = "idle" if motion[secs[i]] < idle else "active"
        if st != state:
            out.append({"kind": state, "start": start, "end": secs[i - 1]})
            start, state = secs[i], st
    out.append({"kind": state, "start": start, "end": secs[-1]})
    return out


def max_gap(times, dur):
    pts = [0.0] + sorted(times) + [dur]
    return max(b - a for a, b in zip(pts, pts[1:]))


def cmd_plan(args):
    tl = load_json(args.timeline)
    buckets, dur = tl["buckets"], tl["duration"]
    secs = sorted(int(s) for s in buckets)
    motion = {s: buckets[str(s)]["motion"] for s in secs}

    plan = [(round(i * (dur / args.skeleton) + dur / args.skeleton / 2, 2), "A",
             f"骨架 {dur / args.skeleton:.0f}s 等间隔") for i in range(args.skeleton)]
    # 两端各留一帧防越界抽帧；原来固定 0.2s，对短素材会漏掉可观的尾部
    edge = max(0.05, 1.0 / ((tl.get("info") or {}).get("fps") or 30.0))
    plan += [(round(edge, 2), "A", f"开头 {edge:.2f}s"),
             (round(dur - edge, 2), "A", f"末尾 {edge:.2f}s")]
    skeleton_gap = max_gap([p[0] for p in plan], dur)

    events = []
    for t in tl.get("cuts", []):
        events += [(0, t, "切点"), (0, min(t + 0.4, dur), "切点后 0.4s")]
    vals = sorted(motion.values())
    p95 = vals[int(len(vals) * 0.95)] if vals else 0.0
    p10 = vals[int(len(vals) * 0.10)] if vals else 0.0
    mean_motion = sum(vals) / len(vals) if vals else 0.0
    dynamic = tl.get("motion_p95", 0.0) >= 2.0 or mean_motion >= 0.8
    for s in secs:
        b = buckets[str(s)]
        if b.get("pix_max", 0.0) >= args.pix_thresh:
            events.append((1, s, f"画面内容突变 YMAX={b['pix_max']:.0f}"))
        if b["motion_max"] >= max(p95, 1.0):
            events.append((1, s, f"运动尖峰 {b['motion_max']:.1f}"))
        if dynamic and motion[s] <= p10:
            events.append((2, s, f"运动谷底 {motion[s]:.3f}"))
    for sg in segments(secs, motion):
        if sg["kind"] == "active":
            events.append((2, sg["start"], "活跃段起点"))
            events.append((2, sg["end"], "活跃段终点"))

    budget = max(0, args.max - len(plan))
    burst = args.burst if args.burst > 0 else 3
    budget = budget // burst if burst > 1 else budget
    window = max(1.0, args.window)
    n_win = max(1, int(dur // window) + 1)
    buckets_of_win = {w: [] for w in range(n_win)}
    for ev in events:
        buckets_of_win[min(int(ev[1] // window), n_win - 1)].append(ev)
    for w in buckets_of_win:
        buckets_of_win[w].sort(key=lambda e: (e[0], e[1]))
    cursor = {w: 0 for w in buckets_of_win}

    taken = 0
    progressing = True
    while taken < budget and progressing:
        progressing = False
        for w in range(n_win):
            if taken >= budget:
                break
            lst = buckets_of_win[w]
            while cursor[w] < len(lst):
                pri, t, why = lst[cursor[w]]
                cursor[w] += 1
                if any(abs(t - p[0]) < MIN_GAP for p in plan):
                    continue
                plan.append((round(float(t), 2), "B", f"{why}"))
                taken += 1
                progressing = True
                break

    if burst > 1:
        dense = []
        for t, phase, why in plan:
            if phase == "B":
                for k in range(burst):
                    dense.append((round(t + k * args.burst_dt, 2), "B",
                                  f"{why} 组{k + 1}/{burst}"))
            else:
                dense.append((t, phase, why))
        kept, seen = [], []
        for item in sorted(dense, key=lambda p: p[0]):
            if item[0] > dur:
                continue
            if any(abs(item[0] - s) < 0.04 for s in seen):
                continue
            seen.append(item[0])
            kept.append(item)
        plan = kept

    plan.sort(key=lambda p: p[0])
    times = [p[0] for p in plan]
    final_gap = max_gap(times, dur)
    cols = COLS_FOR.get(args.per_sheet, 3)
    sheets = (len(plan) + args.per_sheet - 1) // args.per_sheet

    print(f"时长 {dur:.0f}s  活跃段 {sum(1 for s in segments(secs, motion) if s['kind'] == 'active')} 个")
    fps = tl.get("info", {}).get("fps") or 0
    print(f"采样体制：{'高运动' if dynamic else '低运动'}  运动均值 {mean_motion:.3f}  p95 {p95:.3f}")
    print(f"事件处成组 {burst} 帧 @{args.burst_dt}s（相邻帧号相差约 "
          f"{args.burst_dt * fps:.0f} 帧）——按帧号顺序读即得运动")
    print(f"骨架 {args.skeleton} 帧（间隔 {dur / args.skeleton:.2f}s，盲区上界 {skeleton_gap:.2f}s）"
          f" + 事件 {taken} 处 → 共 {len(plan)} 帧")
    print(f"最终最大盲区 {final_gap:.2f}s  {sheets} 张 {cols}x{args.per_sheet // cols} 图版"
          f" ≈ {sheets * TOKENS_PER_IMAGE} token")
    per_win = {w: sum(1 for p in plan if p[1] == "B" and int(p[0] // window) == w)
               for w in range(n_win)}
    print(f"事件按 {window:.0f}s 窗口轮转分配：{per_win}")
    print("抽样计划：")
    for t, phase, why in plan:
        print(f"  {t:9.2f} | {phase} | {why:<26} | 运动 {motion.get(int(t), 0.0):.2f}")

    out = {"media": tl.get("media"), "duration": dur, "skeleton": args.skeleton,
           "skeleton_gap": round(skeleton_gap, 2), "max_gap": round(final_gap, 2),
           "regime": "dynamic" if dynamic else "static", "burst": burst,
           "fps": fps, "burst_frames_apart": round(args.burst_dt * fps),
           "per_sheet": args.per_sheet, "cols": cols,
           "sheets": sheets, "tokens_est": sheets * TOKENS_PER_IMAGE,
           "shots": [{"index": i + 1, "t": t, "frame": int(round(t * fps)),
                      "phase": p, "reason": w}
                     for i, (t, p, w) in enumerate(plan)]}
    if args.out:
        path = args.out
    else:
        base = out_dir(args, "vw_plan",
                       fallback=os.path.dirname(os.path.abspath(args.timeline)))
        path = os.path.join(base, "shotlist.json")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    print(f"→ {path}")


def extract(ffmpeg, media, t, dest, width, label=None, font=None, src_w=None):
    vf = f"scale={width}:-2:flags={fit_filter(src_w, width)}"
    if label is not None:
        vf += (f",drawtext=fontfile={font}:text='{label}':x=6:y=4:"
               f"fontsize={max(18, width // 8)}:fontcolor=yellow:box=1:boxcolor=black@0.65")
    run([ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", media,
         "-frames:v", "1", "-vf", vf, "-y", dest])
    if not os.path.exists(dest):
        die(f"t={t:.3f}s 处没有抽到帧，请确认该时间是否超出了素材时长")


def cmd_sheet(args):
    ffmpeg = resolve_ffmpeg()
    sl = load_json(args.shotlist)
    media = os.path.abspath(args.media)
    outdir = out_dir(args, "vw_sheets")
    tiledir = os.path.join(outdir, "tiles")
    os.makedirs(tiledir, exist_ok=True)

    per_sheet = sl.get("per_sheet", 9)
    cols = sl.get("cols", COLS_FOR.get(per_sheet, 3))
    rows = (per_sheet + cols - 1) // cols
    tile_w = SHEET_PX // cols - 2
    src_info = ffprobe_info(ffmpeg, media)
    print(scale_note(src_info["width"], tile_w))
    shots = sl["shots"]
    strip = None
    if args.strip:
        try:
            strip = [int(v) for v in args.strip.split(",")]
            if len(strip) != 4:
                raise ValueError
        except ValueError:
            die("--strip 需要 x,y,w,h 四个整数")
    per_strip = per_sheet
    if strip:
        sx, sy, sw, sh = strip
        sz = args.strip_zoom
        per_strip = max(2, min(per_sheet,
                               int(8 * sw / max(1, sh)),
                               int(640000 / max(1, sw * sz * sh * sz))))
        print(f"条带每张 {per_strip} 格（受单图 64 万像素与 8:1 宽高比约束）")
    step = per_strip if strip else per_sheet
    font = None if strip else resolve_font()

    manifest = {"media": os.path.basename(media), "per_sheet": per_sheet, "cols": cols,
                "rows": rows, "tile_width": tile_w, "mode": "strip" if strip else
                ("diff" if args.diff else "plain"), "sheets": []}

    strip_paths = []
    for si in range(0, len(shots), step):
        group = shots[si:si + step]
        paths = []
        for gi, shot in enumerate(group):
            if strip:
                break
            p = os.path.join(tiledir, f"s{si // step + 1:02d}_{gi:02d}.png")
            if args.diff:
                a = os.path.join(tiledir, f"_a{si + gi}.png")
                b = os.path.join(tiledir, f"_b{si + gi}.png")
                last = max(0.0, sl["duration"] - 0.1)
                extract(ffmpeg, media, min(shot["t"], last), a, tile_w,
                        label=str(shot["index"]), font=font, src_w=src_info["width"])
                extract(ffmpeg, media, min(shot["t"] + args.diff, last), b, tile_w,
                        src_w=src_info["width"])
                magick("convert", a, b, "-compose", "difference", "-composite", p)
                os.remove(a)
                os.remove(b)
            else:
                extract(ffmpeg, media, shot["t"], p, tile_w, label=str(shot["index"]),
                        font=font, src_w=src_info["width"])
            paths.append(p)
        if strip:
            sx, sy, sw, sh = strip
            zoom = args.strip_zoom
            cropped = []
            for i, shot in enumerate(group):
                c = os.path.join(tiledir, f"c{si // step + 1:02d}_{i:02d}.png")
                run([ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{shot['t']:.3f}",
                     "-i", media, "-frames:v", "1", "-vf",
                     f"crop={sw}:{sh}:{sx}:{sy},scale=iw*{zoom}:ih*{zoom}", "-y", c])
                cropped.append(c)
            strip_img = os.path.join(outdir, f"strip_{si // step + 1:02d}.png")
            magick("convert", *cropped, "-append", strip_img)
            strip_paths.append(strip_img)
            for c in cropped:
                os.remove(c)
            manifest["sheets"].append({
                "sheet": strip_img, "kind": "strip",
                "tiles": [{"pos": gi + 1, "t": s["t"], "reason": s["reason"]}
                          for gi, s in enumerate(group)]})
            continue
        sheet = os.path.join(outdir, f"sheet_{si // step + 1:02d}.png")
        magick("montage", *paths, "-tile", f"{cols}x{rows}",
               "-geometry", "+2+2", "-background", "#111", sheet)
        manifest["sheets"].append({
            "sheet": sheet,
            "tiles": [{"row": gi // cols + 1, "col": gi % cols + 1, "index": s["index"],
                       "t": s["t"], "frame": s.get("frame"), "reason": s["reason"]}
                      for gi, s in enumerate(group)]})

    mpath = os.path.join(outdir, "manifest.json")
    with open(mpath, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=1)

    print("位置映射（权威表：位置 → 帧号 → 时间；按帧号顺序读即得运动）：")
    for sh in manifest["sheets"]:
        print(f"\n{os.path.basename(sh['sheet'])}")
        for t in sh["tiles"]:
            fr = f"帧{t['frame']}" if t.get("frame") is not None else "帧?"
            if "row" in t:
                print(f"  第{t['row']}行第{t['col']}列 (#{t['index']}) {fr} → "
                      f"t={t['t']:>9.2f}s  {t['reason']}")
            else:
                print(f"  第{t.get('pos')}格 {fr} → t={t['t']:>9.2f}s  {t['reason']}")
    print(f"\n→ {mpath}")
    if strip_paths:
        print(f"条带：{', '.join(os.path.basename(p) for p in strip_paths)}")


def cmd_gif(args):
    try:
        from PIL import Image, ImageSequence
    except ImportError:
        die("gif 子命令需要 Pillow：pip install pillow")
    src = os.path.abspath(args.media)
    if not os.path.exists(src):
        die(f"找不到文件 {src}")
    outdir = out_dir(args, "vw_gif")
    os.makedirs(outdir, exist_ok=True)

    img = Image.open(src)
    frames, t = [], 0.0
    for page in ImageSequence.Iterator(img):
        delay = page.info.get("duration", img.info.get("duration", 100)) or 100
        frames.append({"index": len(frames), "start": round(t, 3),
                       "delay_ms": int(delay), "image": page.convert("RGBA").copy()})
        t += delay / 1000.0
    total = t
    loop = img.info.get("loop", 0)
    print(f"动图 {os.path.basename(src)}  {img.width}x{img.height}  "
          f"{len(frames)} 帧  实际时长 {total:.2f}s  循环 {loop}")
    if len(frames) > 1:
        delays = sorted(f["delay_ms"] for f in frames)
        print(f"帧延迟 ms：min {delays[0]} 中位 {delays[len(delays) // 2]} max {delays[-1]}"
              f"  → 时间轴按延迟累计，不按帧号")

    picks = frames
    if len(frames) > args.max_frames:
        step = len(frames) / args.max_frames
        picks = [frames[int(i * step)] for i in range(args.max_frames)]
    cols = COLS_FOR.get(9, 3)
    tile_w = SHEET_PX // cols - 2
    tiles, mapping = [], []
    for gi, f in enumerate(picks):
        p = os.path.join(outdir, f"g{gi:02d}.png")
        target = (tile_w, max(1, round(tile_w * img.height / img.width)))
        f["image"].convert("RGB").resize(target, Image.LANCZOS).save(p)
        tiles.append(p)
        mapping.append({"row": gi // cols + 1, "col": gi % cols + 1,
                        "frame": f["index"], "t": f["start"], "delay_ms": f["delay_ms"]})
    sheets = []
    for si in range(0, len(tiles), 9):
        rows = (len(tiles[si:si + 9]) + cols - 1) // cols
        sh = os.path.join(outdir, f"gif_sheet_{si // 9 + 1:02d}.png")
        magick("montage", *tiles[si:si + 9], "-tile", f"{cols}x{rows}",
               "-geometry", "+2+2", "-background", "#111", sh)
        sheets.append(sh)

    manifest = {"media": os.path.basename(src), "frames": len(frames),
                "duration": round(total, 3), "loop": loop, "cols": cols,
                "sampled": len(picks), "sheets": sheets, "tiles": mapping}
    mpath = os.path.join(outdir, "manifest.json")
    with open(mpath, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=1)
    print("位置映射：")
    for m in mapping:
        print(f"  第{m['row']}行第{m['col']}列 → 帧{m['frame']} t={m['t']:.2f}s "
              f"(延迟 {m['delay_ms']}ms)")
    print(f"→ {mpath}  {len(sheets)} 张图版")


def build_segments(tl, min_len=4.0):
    """把时间轴切成连贯段落：以切点与活跃/静止边界为锚，过短的段并入邻段。"""
    buckets, dur = tl["buckets"], tl["duration"]
    secs = sorted(int(s) for s in buckets)
    motion = {s: buckets[str(s)]["motion"] for s in secs}
    marks = {0.0, dur}
    for sg in segments(secs, motion):
        marks.add(float(sg["start"]))
        marks.add(float(sg["end"]))
    for c in tl.get("cuts", []):
        marks.add(float(c))
    bounds = sorted(marks)
    raw = []
    for a, b in zip(bounds, bounds[1:]):
        mid = (a + b) / 2
        kind = "active" if motion.get(int(mid), 0.0) >= IDLE_MOTION else "idle"
        raw.append({"start": a, "end": b, "kind": kind})
    merged = []
    for seg in raw:
        if merged and (seg["end"] - seg["start"] < min_len or seg["kind"] == merged[-1]["kind"]):
            merged[-1]["end"] = seg["end"]
        else:
            merged.append(dict(seg))
    for seg in merged:
        seg["cuts"] = [c for c in tl.get("cuts", []) if seg["start"] <= c < seg["end"]]
        seg["peak"] = max((buckets[str(s)]["motion_max"] for s in secs
                           if seg["start"] <= s < seg["end"]), default=0.0)
    return merged


def tiles_in_range(manifest, start, end):
    hits = []
    for sh in manifest.get("sheets", []):
        for t in sh["tiles"]:
            if start <= t["t"] < end:
                pos = (f"第{t['row']}行第{t['col']}列" if "row" in t else f"第{t.get('pos')}格")
                hits.append({"sheet": os.path.basename(sh["sheet"]), "pos": pos,
                             "index": t.get("index"), "t": t["t"]})
    return hits


def tiles_brief(manifest, start, end, limit=3):
    """段落可看位置：太长就只报前几处，避免交付里堆一屏坐标。"""
    hits = tiles_in_range(manifest, start, end) if manifest else []
    if not hits:
        return "—"
    head = "、".join(f"{h['sheet']} {h['pos']}" for h in hits[:limit])
    return head if len(hits) <= limit else f"{head} 等 {len(hits)} 处"


def manifest_sections(manifest):
    """把 manifest 归一成 [(标题, 条目列表)]；识别不了则返回 None。

    三种来源结构不同：sheet 用 sheets[].tiles，read 用 sheets[].panels，grid 用顶层 tiles。
    """
    sheets = manifest.get("sheets")
    if sheets and isinstance(sheets[0], dict):
        return [(os.path.basename(sh.get("sheet", "图版")),
                 sh.get("tiles") or sh.get("panels") or []) for sh in sheets]
    if sheets:
        return None
    tiles = manifest.get("tiles")
    if tiles:
        cols = manifest.get("cols") or 1
        rows = [{"row": t.get("row", i // cols + 1), "col": t.get("col", i % cols + 1),
                 "index": t.get("index", i + 1), "t": t["t"]} for i, t in enumerate(tiles)]
        return [(os.path.basename(manifest.get("sheet") or "网格"), rows)]
    return None


PANEL_PRESETS = {"small": (500, 140), "medium": (620, 170), "large": (760, 210)}


def est_cell_px(pw, ph, pack, budget=381):
    """估算面板被投影到视觉网格后每格覆盖多少原始像素——读字可行性的判据。"""
    aspect = (ph * pack) / pw
    grid_w = max(1, int((budget / aspect) ** 0.5))
    return pw / grid_w


def fit_filter(src_w, target_w):
    """按缩放方向选重采样。

    实测（4K 近奈奎斯特条纹 → 149px，残留摩尔纹标准差）：
      neighbor 0.500（灾难）、area 0.309、lanczos 0.289、bicubic 0.266。
    所以缩小沿用 bicubic（显式写死，避免依赖默认值），放大用 lanczos。
    """
    if not src_w or not target_w:
        return "bicubic"
    if target_w > src_w * 1.2:
        return "lanczos"
    return "bicubic"


def scale_note(src_w, target_w):
    """把"这一档丢了多少分辨率"写成一句话——4K 在 5x5 档位下与 720p 等价。"""
    if not src_w or not target_w:
        return ""
    ratio = src_w / target_w
    if ratio < 1.05:
        return f"源 {src_w}px → 每格 {target_w}px（未降采样）"
    return (f"源 {src_w}px → 每格 {target_w}px（{ratio:.1f}× 降采样，"
            f"该档位下源分辨率高于此即被丢弃）")


def cmd_grid(args):
    """时序网格：把 N 帧铺成一张图（3x3 / 5x5 / 6x6）。

    这是"粗看"与"读动作"共用的主力档位——用每帧的分辨率换时序密度：
      全片 25 帧 5x5  → 一张图看完结构（谁在场、何时变化），成本约 350 token
      连续 25 帧 5x5  → 动作显微镜（0.06s 一帧），动作链比 0.25s 的稀疏采样清楚得多
    每格越小越糊，但**时序密度决定动作读不读得出来**，这两件事是分开的。
    """
    ffmpeg = resolve_ffmpeg()
    media = os.path.abspath(args.media)
    outdir = out_dir(args, "vw_grid")
    tiledir = os.path.join(outdir, "tiles")
    os.makedirs(tiledir, exist_ok=True)
    info = ffprobe_info(ffmpeg, media)

    t0 = args.t0
    t1 = args.t1 if args.t1 is not None else info["duration"]
    n = args.frames
    step = (t1 - t0) / max(1, n - 1)
    cols = args.cols
    rows = (n + cols - 1) // cols
    tile_w = SHEET_PX // cols - 2
    font = resolve_font()

    tiles, mapping = [], []
    last = max(0.0, info["duration"] - 0.1)
    flags = fit_filter(info["width"], tile_w)
    pre = ""
    if args.region:
        try:
            gx, gy, gw, gh = [int(v) for v in args.region.split(",")]
        except ValueError:
            die("--region 需要 x,y,w,h 四个整数")
        pre = f"crop={gw}:{gh}:{gx}:{gy},"
        flags = fit_filter(gw, tile_w)
    for i in range(n):
        t = min(t0 + i * step, last)
        p = os.path.join(tiledir, f"g{i:03d}.png")
        run([ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", media,
             "-frames:v", "1", "-vf",
             f"{pre}scale={tile_w}:-2:flags={flags},"
             f"drawtext=fontfile={font}:text='{i + 1}':x=4:y=2:"
             f"fontsize={max(18, tile_w // 8)}:fontcolor=yellow:box=1:boxcolor=black@0.65",
             "-y", p])
        if not os.path.exists(p):
            continue
        tiles.append(p)
        mapping.append({"index": i + 1, "row": i // cols + 1, "col": i % cols + 1,
                        "t": round(t, 3), "frame": int(round(t * info["fps"]))})
    rows = (len(tiles) + cols - 1) // cols or 1

    sheet = os.path.join(outdir, "grid.png")
    magick("montage", *tiles, "-tile", f"{cols}x{rows}",
           "-geometry", "+2+2", "-background", "#111", sheet)
    manifest = {"media": os.path.basename(media), "mode": "grid", "cols": cols, "rows": rows,
                "t0": t0, "t1": round(t1, 3), "step_s": round(step, 4),
                "step_frames": round(step * info["fps"]), "sheet": os.path.basename(sheet),
                "tiles": mapping}
    mpath = os.path.join(outdir, "manifest.json")
    with open(mpath, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=1)

    print(f"网格 {cols}x{rows}  取 {len(mapping)} 帧  {t0}~{t1:.2f}s  "
          f"间隔 {step:.3f}s ≈ {step * info['fps']:.1f} 帧")
    src_w = gw if args.region else info["width"]
    print(scale_note(src_w, tile_w) + f"  滤波 {flags}")
    print(f"→ {sheet}")
    print("位置→帧号→时间（按行读，即时序）：")
    for m in mapping:
        print(f"  第{m['row']}行第{m['col']}列 (#{m['index']:>2}) 帧{m['frame']:>4} t={m['t']:>7.2f}s")


def cmd_seq(args):
    """帧号排序的区域序列 —— 运动靠它读：把同一块地方按帧号前后排好，顺序看即得运动。

    每格左上角烧的是**帧序号（1,2,3…）**而非时间码：序号短、大、不会读错，
    真正的帧号与时间走 manifest。相邻两格相差多少帧由 --step 决定，
    这个间隔决定能否看清多快的动作（间隔太稀 = 时间混叠，中间过程被跳过）。
    """
    ffmpeg = resolve_ffmpeg()
    media = os.path.abspath(args.media)
    outdir = out_dir(args, "vw_seq")
    tiledir = os.path.join(outdir, "seq_tiles")
    os.makedirs(tiledir, exist_ok=True)
    info = ffprobe_info(ffmpeg, media)

    if args.times:
        times = [float(v) for v in args.times.split(",")]
    elif args.center is not None:
        n = args.count
        times = [args.center + (i - n // 2) * args.step for i in range(n)]
        times = [t for t in times if 0 <= t <= info["duration"]]
    else:
        die("需要 --times 或 --center")

    if args.region:
        try:
            rx, ry, rw, rh = [int(v) for v in args.region.split(",")]
        except ValueError:
            die("--region 需要 x,y,w,h 四个整数")
    else:
        rx, ry, rw, rh = 0, 0, info["width"], info["height"]

    z = args.zoom
    per_strip = max(2, min(len(times), int(8 * rw / rh), int(640000 / max(1, rw * z * rh * z))))
    font = resolve_font()
    strips, mapping = [], []
    for start in range(0, len(times), per_strip):
        group = times[start:start + per_strip]
        tiles = []
        for i, t in enumerate(group):
            p = os.path.join(tiledir, f"s{start + i:04d}.png")
            label = str(start + i + 1)
            run([ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", media,
                 "-frames:v", "1", "-vf",
                 f"crop={rw}:{rh}:{rx}:{ry},scale=iw*{z}:ih*{z}:flags=lanczos,"
                 f"drawtext=fontfile={font}:text='{label}':x=4:y=2:"
                 f"fontsize={max(20, rw * z // 10)}:fontcolor=yellow:box=1:boxcolor=black@0.7",
                 "-y", p])
            tiles.append(p)
        strip = os.path.join(outdir, f"seq_{start // per_strip + 1:02d}.png")
        magick("convert", *tiles, "-append", strip)
        strips.append(os.path.basename(strip))
        for i, t in enumerate(group):
            mapping.append({"seq": start + i + 1, "t": round(t, 3),
                            "frame": int(round(t * info["fps"])),
                            "strip": os.path.basename(strip),
                            "pos_in_strip": i + 1})

    manifest = {"media": os.path.basename(media), "region": f"{rx},{ry},{rw},{rh}",
                "zoom": z, "fps": info["fps"], "step_s": args.step,
                "step_frames": round(args.step * info["fps"]),
                "strips": strips, "sequence": mapping}
    mpath = os.path.join(outdir, "manifest.json")
    with open(mpath, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=1)

    print(f"序列 {len(times)} 帧  相邻间隔 {args.step}s ≈ {args.step * info['fps']:.0f} 帧  "
          f"区域 {rw}x{rh} ×{z}  → {len(strips)} 条")
    for s in strips:
        print(f"  {s}")
    print("逐条自上而下按序号读；序号 → 帧号 → 时间见 manifest")
    print(f"→ {mpath}")


def cmd_read(args):
    """按"可读分辨率"切面板供视觉直接阅读 —— 读字的主通道是眼睛，不是 OCR。

    格子在图片被投影到 18x18 网格时的覆盖像素决定了小字能不能读：
    整帧 1284 宽时每格约 64px（18px 字读不了），切成 620x170 面板后每格约 17px（能读）。
    拼版会按比例摊薄这个预算，--pack 越大越省 token 也越糊。
    """
    ffmpeg = resolve_ffmpeg()
    media = os.path.abspath(args.media)
    outdir = out_dir(args, "vw_read")
    tiledir = os.path.join(outdir, "panels")
    os.makedirs(tiledir, exist_ok=True)

    info = ffprobe_info(ffmpeg, media)
    if args.region:
        try:
            rx, ry, rw, rh = [int(v) for v in args.region.split(",")]
        except ValueError:
            die("--region 需要 x,y,w,h 四个整数")
    else:
        rx, ry, rw, rh = 0, 0, info["width"], info["height"]

    if args.times:
        times = [float(v) for v in args.times.split(",")]
    else:
        sl = load_json(args.shotlist)
        times = [s["t"] for s in sl["shots"]]

    pw, ph = PANEL_PRESETS[args.panel]
    zoom = args.zoom
    cols = max(1, (rw + pw - 1) // pw)
    rows = max(1, (rh + ph - 1) // ph)
    tiles, mapping, skipped = [], [], 0
    for t in times:
        for r in range(rows):
            for c in range(cols):
                x = min(rx + c * pw, rx + rw - 1)
                y = min(ry + r * ph, ry + rh - 1)
                w = min(pw, rx + rw - x)
                h = min(ph, ry + rh - y)
                if w < 40 or h < 20:
                    continue
                out = os.path.join(tiledir, f"t{t:07.2f}_r{r:02d}c{c:02d}.png")
                run([ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{t:.3f}",
                     "-i", media, "-frames:v", "1", "-vf",
                     f"crop={w}:{h}:{x}:{y},scale=iw*{zoom}:ih*{zoom}", "-y", out])
                sd = float(magick("identify", "-format",
                                  "%[fx:standard_deviation]", out).stdout or 0)
                if sd < 0.005:
                    os.remove(out)
                    skipped += 1
                    continue
                tiles.append({"path": out, "t": t, "row": r, "col": c,
                              "frame": int(round(t * info["fps"])),
                              "region": f"x={x} y={y} w={w} h={h}"})
                mapping.append({"t": round(t, 2), "row": r + 1, "col": c + 1,
                                "frame": int(round(t * info["fps"])),
                                "region": f"x={x} y={y} w={w} h={h}"})

    pack = max(1, min(4, args.pack))
    sheets = []
    for si in range(0, len(tiles), pack):
        group = tiles[si:si + pack]
        sheet = os.path.join(outdir, f"read_{si // pack + 1:02d}.png")
        magick("montage", *[g["path"] for g in group], "-tile", "1x",
               "-geometry", "+0+4", "-background", "#888", sheet)
        sheets.append({"sheet": os.path.basename(sheet),
                       "panels": [{"t": g["t"], "row": g["row"] + 1, "col": g["col"] + 1}
                                  for g in group]})

    manifest = {"media": os.path.basename(media), "region": f"{rx},{ry},{rw},{rh}",
                "panel": f"{pw}x{ph}", "zoom": zoom, "pack": pack,
                "grid": f"{rows}行×{cols}列", "sheets": sheets, "panels": mapping}
    mpath = os.path.join(outdir, "manifest.json")
    with open(mpath, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=1)

    cell = est_cell_px(pw, ph, pack)
    print(f"区域 {rw}x{rh} → 面板 {pw}x{ph} ×{zoom}  网格 {rows}行×{cols}列"
          f"  跳过空白 {skipped} 块")
    print(f"每图 {pack} 面板：每格覆盖约 {cell:.0f}px"
          f"（≤20px 才读得动 18px 小字；拼版越多越糊）")
    print(f"共 {len(tiles)} 面板 → {len(sheets)} 张图")
    for sh in sheets:
        print(f"  {sh['sheet']}: " + "、".join(
            f"t={p['t']:.1f}s 第{p['row']}行第{p['col']}列" for p in sh["panels"]))
    print(f"→ {mpath}")
    print("读法：逐张看；引用时按 manifest 的 t 与行列定位")


def cmd_asr(args):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        die("asr 子命令需要 faster-whisper：pip install faster-whisper")
    media = os.path.abspath(args.media)
    if not os.path.exists(media):
        die(f"找不到文件 {media}")
    outdir = out_dir(args, "vw_asr", fallback=os.path.dirname(media))
    os.makedirs(outdir, exist_ok=True)
    print(f"加载 faster-whisper 模型 {args.model}（CPU int8）…")
    model = WhisperModel(args.model, device="cpu", compute_type="int8")
    segs, info = model.transcribe(media, language=args.lang or None,
                                 vad_filter=True, beam_size=5)
    print(f"检测语言 {info.language}（置信 {info.language_probability:.2f}）")
    manifest = load_json(args.manifest) if args.manifest else None
    rows = []
    for s in segs:
        text = s.text.strip()
        if not text:
            continue
        row = {"start": round(s.start, 2), "end": round(s.end, 2), "text": text}
        if manifest:
            row["tiles"] = tiles_in_range(manifest, row["start"], row["end"])
        rows.append(row)
        print(f"  [{row['start']:8.2f} → {row['end']:8.2f}] {text}")
    path = os.path.join(outdir, "asr.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"media": os.path.basename(media), "model": args.model,
                   "language": info.language, "segments": rows}, fh, ensure_ascii=False, indent=1)
    print(f"{len(rows)} 段 → {path}")


def _norm(text):
    return re.sub(r"[\s，。、！？；：,.!?;:\"'（）()\[\]【】]", "", text)


def cmd_ocr(args):
    print("提示：本地库 OCR 是有损二手通道，读字请优先用 `vw.py read` 切面板后自己看。")
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        die("ocr 子命令需要 rapidocr-onnxruntime：pip install rapidocr-onnxruntime")
    ffmpeg = resolve_ffmpeg()
    sl = load_json(args.shotlist)
    media = os.path.abspath(args.media)
    outdir = out_dir(args, "vw_ocr")
    os.makedirs(outdir, exist_ok=True)
    engine = RapidOCR()
    manifest = load_json(args.manifest) if args.manifest else None

    vf = []
    if args.region:
        try:
            x, y, w, h = [int(v) for v in args.region.split(",")]
        except ValueError:
            die("--region 需要 x,y,w,h 四个整数")
        vf.append(f"crop={w}:{h}:{x}:{y}")
    if args.zoom != 1:
        vf.append(f"scale=iw*{args.zoom}:ih*{args.zoom}")
    vf = ",".join(vf) if vf else "null"

    rows = []
    for shot in sl["shots"]:
        f = os.path.join(outdir, f"ocr_{shot['index']:03d}.png")
        run([ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{shot['t']:.3f}",
             "-i", media, "-frames:v", "1", "-vf", vf, "-y", f])
        res, _ = engine(f)
        text = " ".join(r[1] for r in res) if res else ""
        rows.append({"t": shot["t"], "text": text})
        os.remove(f)

    import difflib
    merged = []
    for r in rows:
        if not _norm(r["text"]):
            continue
        if merged and difflib.SequenceMatcher(
                None, _norm(merged[-1]["text"]), _norm(r["text"])).ratio() >= args.similar:
            merged[-1]["end"] = r["t"]
            merged[-1]["samples"] += 1
            continue
        merged.append({"start": r["t"], "end": r["t"], "text": r["text"], "samples": 1})
    for m in merged:
        m["start"] = round(m["start"], 2)
        m["end"] = round(m["end"], 2)
        if manifest:
            m["tiles"] = tiles_in_range(manifest, m["start"], m["end"] + 0.01)

    path = os.path.join(outdir, "ocr.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"media": os.path.basename(media), "engine": "rapidocr_onnxruntime",
                   "region": args.region, "zoom": args.zoom, "segments": merged},
                  fh, ensure_ascii=False, indent=1)
    print(f"抽样 {len(rows)} 帧 → 归并 {len(merged)} 段重复文字")
    for m in merged[:40]:
        print(f"  [{m['start']:8.2f} → {m['end']:8.2f}] ({m['samples']} 样本) {m['text'][:60]}")
    print(f"→ {path}")
    print("注意：机器 OCR 提供覆盖，引用前须裁切放大用视觉复核")


def cmd_doctor(args):
    """依赖自检：逐项报出外部程序与可选库的状态，缺什么、怎么补。"""
    print(f"python      {sys.version.split()[0]}  [{sys.platform}]")

    ffmpeg = None
    for cand, state, missing in scan_ffmpeg():
        if state == "ok":
            ffmpeg = cand
            print(f"ffmpeg      {cand}")
            print(f"            {_version_line([cand, '-version']) or ''}")
            break
        if state == "path":
            print(f"ffmpeg      {cand}  路径不存在")
        elif state == "exec":
            print(f"ffmpeg      {cand}  无法执行")
        else:
            print(f"ffmpeg      {cand}  缺滤镜 {', '.join(missing)}")
    if ffmpeg is None:
        print("ffmpeg      不可用：需要带 scdet/freezedetect/drawtext 的完整构建，"
              "见 README 安装一节；已装但不合格时用 VW_FFMPEG 指定")
    else:
        probe = find_ffprobe(ffmpeg)
        if probe:
            print(f"ffprobe     {probe}")
        else:
            print("ffprobe     找不到，用 VW_FFPROBE 指定")

    im = find_magick()
    if im:
        major, exe = im
        kind = "magick 聚合入口" if major >= 7 else "6.x 独立命令"
        print(f"magick      ImageMagick {major}.x  {kind}  {exe}".rstrip())
    else:
        print("magick      找不到，用 VW_MAGICK 指定")

    font = find_font()
    if font:
        print(f"font        {font}")
    else:
        print("font        找不到，用 VW_FONT 指定 .ttf/.ttc")

    for label, mod, note in (("Pillow", "PIL", "gif 子命令"),
                             ("numpy", "numpy", "vwtools 专用脚本"),
                             ("faster-whisper", "faster_whisper", "asr 子命令"),
                             ("rapidocr", "rapidocr_onnxruntime", "ocr 子命令")):
        try:
            __import__(mod)
            print(f"{label:<15} OK     {note}")
        except ImportError:
            print(f"{label:<15} 未装   {note}")

    if ffmpeg and im and font and find_ffprobe(ffmpeg):
        print("\n必需项就绪：probe / plan / sheet / grid / seq / read / report 可用")
    else:
        print("\n有必需项缺失：按上面提示补齐后再跑")


def cmd_init(args):
    """把探测结果写成配置文件，省去逐次设环境变量。"""
    data = {}
    for cand, state, _ in scan_ffmpeg():
        if state == "ok":
            data["ffmpeg"] = cand
            break
    probe = find_ffprobe(data.get("ffmpeg"))
    if probe:
        data["ffprobe"] = probe
    im = find_magick()
    if im and im[1]:
        data["magick"] = im[1]
    font = find_font()
    if font:
        data["font"] = font
    data["outdir"] = ""
    data["defaults"] = {"skeleton": 16, "max": 36, "cols": 5, "panel": "medium",
                        "model": "base", "lang": ""}

    if args.path:
        target = os.path.abspath(args.path)
    elif args.global_:
        target = os.path.join(os.path.expanduser("~"), ".config", "video-watch",
                              CONFIG_NAME)
    else:
        target = os.path.join(os.getcwd(), CONFIG_NAME)

    if os.path.exists(target) and not args.force:
        die(f"{target} 已存在，要覆盖请加 --force")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    print(f"配置已写入 {target}")
    for key in ("ffmpeg", "ffprobe", "magick", "font", "outdir"):
        print(f"  {key:<9} {data.get(key) or '（未探测到，可手动填写）'}")
    print("  另有 defaults 段：skeleton / max / cols / panel / model / lang")


def cmd_report(args):
    tl = load_json(args.timeline)
    sl = load_json(args.shotlist)
    manifest = load_json(args.manifest) if args.manifest else None
    asr = load_json(args.asr) if args.asr else None
    ocr = load_json(args.ocr) if args.ocr else None
    segs = build_segments(tl)

    L = []
    info = tl.get("info", {})
    L.append(f"# 观看交付：{tl.get('media', '素材')}")
    L.append("")
    L.append("> 由 `video-watch` 生成骨架；**带 ※ 的段落需人工视觉填写**。")
    L.append("> 量测数据来自 ffmpeg 逐帧遍历，文字转写为机器输出，引用前须放大复核。")
    L.append("")
    L.append("## 0. 结论摘要 ※")
    L.append("")
    L.append("- （3~5 条，先给观测，再给推断）")
    L.append("")
    L.append("## 1. 完整描述")
    L.append("")
    L.append(f"- 素材：{tl.get('media')}  {info.get('width')}×{info.get('height')} "
             f"{info.get('fps')}fps  {tl.get('duration'):.1f}s  "
             f"编码 {info.get('video_codec')} / 音频 {info.get('audio_codec') or '无'}")
    L.append(f"- 帧数 {tl.get('frames')}，逐帧遍历无采样")
    L.append(f"- 内容判型：**{tl.get('content_kind')}**")
    L.append(f"- 口径提示：{tl.get('advice')}")
    L.append(f"- 切点 {len(tl.get('cuts', []))} 个，冻结 {len(tl.get('freezes', []))} 处，"
             f"静音事件 {len(tl.get('silences', []))} 个")
    L.append(f"- 采样 {len(sl['shots'])} 帧覆盖全片，最大盲区 **{sl.get('max_gap')}s**"
             f"（骨架盲区上界 {sl.get('skeleton_gap')}s）")
    L.append(f"- 视觉开销：{sl.get('sheets')} 张图版 ≈ {sl.get('tokens_est')} token")
    L.append("")
    L.append("### 全片段落（量测层给出，含对应图版位置）")
    L.append("")
    L.append("| 起 | 止 | 时长 | 性质 | 段内切点 | 峰值运动 | 可看的图版位置 |")
    L.append("|---|---|---|---|---|---|---|")
    for s in segs:
        kind = "活动" if s["kind"] == "active" else "静止"
        cuts = ",".join(f"{c:.1f}" for c in s["cuts"]) or "—"
        pos = tiles_brief(manifest, s["start"], s["end"])
        L.append(f"| {s['start']:.1f} | {s['end']:.1f} | {s['end'] - s['start']:.1f}s | {kind} "
                 f"| {cuts} | {s['peak']:.1f} | {pos} |")
    L.append("")

    L.append("## 2. 文字内容转写（带时间节点）")
    L.append("")
    L.append("> 按需转写：仅当用户要求、或量测证据显示确有需要转写的文本时才做；"
             "否则本节省略。转写为机器输出，引用前须裁切放大复核。")
    L.append("")
    if ocr:
        L.append("### 2.1 画面文字（机器 OCR，未逐字核实）")
        L.append("")
        L.append("| 起 | 止 | 文字 | 样本数 | 对应画面 |")
        L.append("|---|---|---|---|---|")
        for m in ocr["segments"]:
            tiles = "、".join(f"{h['sheet']} {h['pos']}" for h in m.get("tiles", [])) or "—"
            L.append(f"| {m['start']:.1f} | {m['end']:.1f} | {m['text']} | {m['samples']} | {tiles} |")
        L.append("")
    if asr:
        L.append(f"### 2.2 语音文字（faster-whisper {asr.get('model')}，"
                 f"语言 {asr.get('language')}，未逐字核实）")
        L.append("")
        L.append("| 起 | 止 | 文字 | 对应画面 |")
        L.append("|---|---|---|---|")
        for m in asr["segments"]:
            tiles = "、".join(f"{h['sheet']} {h['pos']}" for h in m.get("tiles", [])) or "—"
            L.append(f"| {m['start']:.1f} | {m['end']:.1f} | {m['text']} | {tiles} |")
        L.append("")
    if not ocr and not asr:
        L.append("（本素材未做文字转写）")
        L.append("")

    L.append("## 3. 部分细节 ※")
    L.append("")
    L.append("按需展开：具体界面元素、文字排版、运动方向、颜色变化、操作序列。")
    L.append("小字必须裁切放大后写，且标注「已放大核实」。")
    L.append("")

    L.append("## 4. 图版与位置映射（权威表）")
    L.append("")
    if manifest:
        sections = manifest_sections(manifest)
        if sections is None:
            L.append("（这份 manifest 没有可展开的图版映射段；"
                     "report 认得 sheet / read / grid 产出的 manifest）")
            L.append("")
        else:
            for title, rows in sections:
                L.append(f"### {title}")
                L.append("")
                for t in rows:
                    idx = f" (#{t['index']})" if t.get("index") is not None else ""
                    why = f"  {t['reason']}" if t.get("reason") else ""
                    if "row" in t:
                        L.append(f"- 第{t['row']}行第{t['col']}列{idx} → "
                                 f"`t={t['t']:.2f}s`{why}")
                    else:
                        L.append(f"- 第{t.get('pos')}格{idx} → `t={t['t']:.2f}s`{why}")
                L.append("")
    else:
        L.append("（未提供 manifest.json）")
        L.append("")

    L.append("## 5. 不确定与边界")
    L.append("")
    L.append(f"- 本片最大盲区 {sl.get('max_gap')}s：盲区内的事件未被眼睛看到，仅由量测层兜底。")
    L.append("- 图版内文字不采信；本交付的文字转写为机器输出，未经逐字复核。")
    L.append("- 先后顺序与时间戳来自量测层；任何「缓慢变化」类描述属推断。")
    L.append("")
    L.append("## 6. 产物清单")
    L.append("")
    for name, path in (("时间轴", args.timeline), ("抽样计划", args.shotlist),
                       ("图版清单", args.manifest), ("OCR", args.ocr), ("ASR", args.asr)):
        if path:
            L.append(f"- {name}：`{os.path.basename(path)}`")
    L.append("")

    if args.out:
        out = args.out
    else:
        base = out_dir(args, "vw_report",
                       fallback=os.path.dirname(os.path.abspath(args.timeline)))
        out = os.path.join(base, "交付-观看报告.md")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))
    print(f"交付骨架 → {out}")
    print(f"段落 {len(segs)} 段；OCR {len(ocr['segments']) if ocr else 0} 段；"
          f"ASR {len(asr['segments']) if asr else 0} 段")


def build_parser():
    ap = argparse.ArgumentParser(prog="vw", description="视频/动图 → 图版序列")
    ap.add_argument("--config", default=None, help="指定配置文件路径")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("probe", help="量测：切点/冻结/运动/静音")
    p.add_argument("media")
    p.add_argument("--out", default=None)
    p.add_argument("--scdet", type=float, default=10.0)
    p.add_argument("--no-audio", action="store_true")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("plan", help="采样：骨架优先的抽样计划")
    p.add_argument("timeline")
    p.add_argument("--skeleton", type=int, default=cfg("skeleton", 16))
    p.add_argument("--max", type=int, default=cfg("max", 36))
    p.add_argument("--window", type=float, default=120.0)
    p.add_argument("--per-sheet", type=int, default=9, choices=sorted(COLS_FOR))
    p.add_argument("--pix-thresh", type=float, default=128.0,
                   help="画面内容突变阈值（差分帧最大像素变化），文档/界面类内容的主信号")
    p.add_argument("--burst", type=int, default=0,
                   help="事件处成组采样帧数；0=按内容自动（高运动 3，低运动 1）")
    p.add_argument("--burst-dt", type=float, default=0.12, help="组内帧间隔秒")
    p.add_argument("--out", default=None)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("sheet", help="图版：联络表 + 位置映射清单")
    p.add_argument("shotlist")
    p.add_argument("media")
    p.add_argument("--out", default=None)
    p.add_argument("--diff", type=float, default=0.0,
                   help="差分模式：与 t+Δ 帧做绝对差，Δ 秒")
    p.add_argument("--strip", default=None, help="定窗条带：x,y,w,h（裁同一区域跨帧纵排）")
    p.add_argument("--strip-zoom", type=int, default=2, help="条带放大倍数（从原始分辨率裁）")
    p.set_defaults(func=cmd_sheet)

    p = sub.add_parser("gif", help="动图：按帧延迟归一化时间轴")
    p.add_argument("media")
    p.add_argument("--out", default=None)
    p.add_argument("--max-frames", type=int, default=18)
    p.set_defaults(func=cmd_gif)

    p = sub.add_parser("grid", help="时序网格：N 帧铺一张图（3x3 / 5x5 / 6x6），粗看与读动作的主力")
    p.add_argument("--media", required=True)
    p.add_argument("--t0", type=float, default=0.0)
    p.add_argument("--t1", type=float, default=None, help="不填则到片尾")
    p.add_argument("--frames", type=int, default=25)
    p.add_argument("--cols", type=int, default=cfg("cols", 5))
    p.add_argument("--region", default=None,
                   help="先裁感兴趣区再铺格 x,y,w,h（大尺寸源下把像素预算花在内容上）")
    p.add_argument("--out", default=None)
    p.set_defaults(func=cmd_grid)

    p = sub.add_parser("seq", help="帧号排序的区域序列（读运动的主通道）")
    p.add_argument("--media", required=True)
    p.add_argument("--times", default=None, help="逗号分隔的时刻，按给定顺序成序列")
    p.add_argument("--center", type=float, default=None, help="以该时刻为中心自动取 count 帧")
    p.add_argument("--count", type=int, default=12)
    p.add_argument("--step", type=float, default=0.1, help="相邻帧间隔秒，决定能否看清快动作")
    p.add_argument("--region", default=None, help="x,y,w,h；默认整帧")
    p.add_argument("--zoom", type=int, default=3)
    p.add_argument("--out", default=None)
    p.set_defaults(func=cmd_seq)

    p = sub.add_parser("read", help="切可读面板供视觉直接读字（读字主通道）")
    p.add_argument("--media", required=True)
    p.add_argument("--times", default=None, help="逗号分隔的时刻；不填则用 shotlist 的全部采样点")
    p.add_argument("--shotlist", default=None)
    p.add_argument("--region", default=None, help="只切该区域 x,y,w,h；默认整帧")
    p.add_argument("--panel", default=cfg("panel", "medium"), choices=sorted(PANEL_PRESETS))
    p.add_argument("--zoom", type=int, default=2)
    p.add_argument("--pack", type=int, default=1, help="每张图放几个面板（1 最清晰，4 最省）")
    p.add_argument("--out", default=None)
    p.set_defaults(func=cmd_read)

    p = sub.add_parser("ocr", help="（不推荐）本地库 OCR，仅作覆盖参考")
    p.add_argument("shotlist")
    p.add_argument("media")
    p.add_argument("--out", default=None)
    p.add_argument("--manifest", default=None)
    p.add_argument("--region", default=None, help="只识别该区域 x,y,w,h（如字幕条/正文区）")
    p.add_argument("--zoom", type=int, default=2, help="识别前放大倍数，小字必须放大")
    p.add_argument("--similar", type=float, default=0.9,
                   help="相邻样本归并为同一段的相似度阈值")
    p.set_defaults(func=cmd_ocr)

    p = sub.add_parser("asr", help="语音文字：faster-whisper 带时间戳转写")
    p.add_argument("media")
    p.add_argument("--out", default=None)
    p.add_argument("--model", default=cfg("model", "base"))
    p.add_argument("--lang", default=cfg("lang", None), help="如 zh；不填则自动检测")
    p.add_argument("--manifest", default=None, help="提供则把每段文字挂到对应图版位置")
    p.set_defaults(func=cmd_asr)

    p = sub.add_parser("report", help="交付：生成观看报告骨架")
    p.add_argument("timeline")
    p.add_argument("shotlist")
    p.add_argument("--manifest", default=None)
    p.add_argument("--ocr", default=None)
    p.add_argument("--asr", default=None)
    p.add_argument("--out", default=None)
    p.set_defaults(func=cmd_report)

    sub.add_parser("doctor", help="依赖自检：报出外部程序与可选库的状态").set_defaults(
        func=cmd_doctor)

    p = sub.add_parser("init", help="初始化：把探测到的依赖路径写成配置文件")
    p.add_argument("--global", dest="global_", action="store_true",
                   help="写到用户级配置（~/.config/video-watch/）")
    p.add_argument("--path", default=None, help="写到指定路径")
    p.add_argument("--force", action="store_true", help="覆盖已存在的配置文件")
    p.set_defaults(func=cmd_init)

    return ap


def config_from_argv():
    """从 argv 取 --config，允许它出现在子命令之后。"""
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--config" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--config="):
            return a.split("=", 1)[1]
    return None


def main():
    # 配置要先于 parser 就绪，子命令的默认值才读得到配置。
    load_config(config_from_argv())
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
