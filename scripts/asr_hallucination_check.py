#!/usr/bin/env python3
"""asr_hallucination_check.py —— 证伪：这条音轨上的转写是"听到了"还是"编出来的"。

问题
----
Whisper 系模型在**没有语音**的音频上不会返回空，它会生成最常见的模板句
（中文常见的是字幕署名格式）。这类输出**看起来完全正常**，极易被当成真实转写。

判定方法（不依赖人工听）
------------------------
同时看三件事：
  1. 模型自报的 `no_speech_prob` —— 高于 0.5 就是模型自己认为"这里没人说话"
  2. **互不重叠的多个时间窗是否输出完全相同的文本** —— 真实语音不可能这样
  3. 全片是否只落在极少数几个模板句上

只要第 2 条命中，无论文本多通顺，都判定为幻觉。

同时给出**对照**：默认（开 VAD）的转写结果。两者一对比就一目了然：
  - 开 VAD 得到 N 段，关 VAD 得到一堆相同模板句 → 音轨无人声
  - 开 VAD 也得到同样的 N 段 → 转写可信

用法
----
    python asr_hallucination_check.py clip.mp4
    python asr_hallucination_check.py clip.mp4 --model small --lang zh --json out.json

依赖：pip install faster-whisper "av<19"
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# 国内环境常见：直连 HuggingFace 超时，且 Xet 传输域名不可达。
# 这里在导入前设好默认值，用户已有的环境变量优先。
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

try:
    from faster_whisper import WhisperModel
except ImportError:
    sys.exit('需要 faster-whisper：pip install faster-whisper "av<19"\n'
             "（av 必须 <19：faster-whisper 1.2.1 用了 PyAV 19 已移除的 metadata_errors 参数）")


def normalize(text: str) -> str:
    return "".join(ch for ch in text.strip() if ch not in " \t\r\n，。、！？；：,.!?;:\"'（）()【】")


def run(media: str, model_name: str, lang: str | None,
        vad: bool, strict: bool) -> list[dict]:
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    kw = dict(language=lang, beam_size=5, vad_filter=vad)
    if strict:
        # 把"这不是语音"的门槛全部拆掉，逼模型无论如何都出字
        kw.update(no_speech_threshold=1.0, log_prob_threshold=-10.0,
                  compression_ratio_threshold=10.0)
    segs, _info = model.transcribe(media, **kw)
    out = []
    for s in segs:
        t = s.text.strip()
        if not t:
            continue
        out.append({"start": round(s.start, 2), "end": round(s.end, 2),
                    "no_speech_prob": round(float(s.no_speech_prob), 3),
                    "avg_logprob": round(float(s.avg_logprob), 2),
                    "text": t})
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="检验 ASR 输出是真转写还是幻觉")
    ap.add_argument("media")
    ap.add_argument("--model", default="small", help="faster-whisper 模型名（默认 small）")
    ap.add_argument("--lang", default="zh", help="语言；留空则自动检测")
    ap.add_argument("--json", dest="json_out", default=None)
    args = ap.parse_args()

    if not os.path.exists(args.media):
        sys.exit(f"找不到文件 {args.media}")
    if not args.lang:
        args.lang = None

    print("== 对照组：默认（开启 VAD）==")
    trusted = run(args.media, args.model, args.lang, vad=True, strict=False)
    for s in trusted:
        print(f"  [{s['start']:8.2f} → {s['end']:8.2f}] {s['text']}")
    print(f"  → {len(trusted)} 段\n")

    print("== 实验组：关闭 VAD + 拆掉所有拒识门槛（强制出字）==")
    forced = run(args.media, args.model, args.lang, vad=False, strict=True)
    for s in forced:
        print(f"  [{s['start']:8.2f} → {s['end']:8.2f}] "
              f"no_speech={s['no_speech_prob']:.3f} logprob={s['avg_logprob']:.2f} | {s['text']!r}")
    print(f"  → {len(forced)} 段\n")

    # ---- 判定 ----
    # 核心判据不是"开/关 VAD 是否逐字相同"，而是**强制模式下是否反复输出同一句**：
    # 幻觉的签名是「时间窗互不重叠、文本却完全一致」；真实语音的每个片段文本都不同。
    print("== 判定 ==")
    result = {"media": os.path.basename(args.media), "model": args.model,
              "vad_segments": trusted, "forced_segments": forced,
              "verdict": None, "reasons": []}

    ftexts = [normalize(s["text"]) for s in forced]
    funique = set(ftexts)
    unique_ratio = (len(funique) / len(ftexts)) if ftexts else 0.0
    avg_ns = (sum(s["no_speech_prob"] for s in forced) / len(forced)) if forced else 0.0

    if not trusted and not forced:
        v = "无人声：两种模式都无输出"
        result["reasons"].append("VAD 与强制模式均未产出文本")
    elif not trusted and forced and len(funique) == 1 and len(forced) >= 2:
        v = "判定为幻觉：音轨很可能没有人声"
        result["reasons"].append(
            f"{len(forced)} 个互不重叠的时间窗输出了完全相同的文本 {sorted(funique)[0]!r}")
        result["reasons"].append(f"模型自报 no_speech 概率均值 {avg_ns:.2f}（它自己也不认为这里有人说话）")
    elif not trusted and forced and avg_ns >= 0.4:
        # VAD（与语言模型无关的语音检测器）什么都没找到，而模型自报"这里没人说话"的概率又很高。
        # 这两条独立证据一致时，"文本看起来通顺"不足以推翻它们——哪怕只产出一段。
        v = "判定为幻觉：音轨很可能没有人声"
        result["reasons"].append("VAD 模式无任何输出（该检测器不依赖语言模型）")
        result["reasons"].append(
            f"强制模式仅产出 {len(forced)} 段，模型自报 no_speech 概率均值 {avg_ns:.2f}")
        result["reasons"].append(
            f"强制模式内容：{[s['text'] for s in forced]!r}（单段文本通顺不构成证据）")
    elif not trusted and unique_ratio <= 0.5 and len(forced) >= 4:
        v = "高度疑似幻觉：全片几乎只落在少数几个模板句上"
        result["reasons"].append(f"{len(forced)} 段里只有 {len(funique)} 种不同文本（unique 比 {unique_ratio:.2f}）")
        result["reasons"].append(f"模型自报 no_speech 概率均值 {avg_ns:.2f}")
    elif trusted and unique_ratio >= 0.8:
        v = "转写可信：有真实语音"
        result["reasons"].append(
            f"VAD 模式得到 {len(trusted)} 段；强制模式 {len(forced)} 段中 {len(funique)} 段文本各不相同"
            f"（unique 比 {unique_ratio:.2f}）——幻觉不会产出这种多样性")
        result["reasons"].append(f"模型自报 no_speech 概率均值仅 {avg_ns:.2f}（低，即认为有人说话）")
    elif trusted:
        v = "转写可信，但存在分段差异，建议复核"
        result["reasons"].append(f"VAD {len(trusted)} 段 / 强制 {len(forced)} 段，unique 比 {unique_ratio:.2f}")
    else:
        v = "无法判定：强制模式有内容但缺乏明确特征"
        result["reasons"].append(f"unique 比 {unique_ratio:.2f}，no_speech 均值 {avg_ns:.2f}")
        result["reasons"].append("请用 scripts/audio_probe.py 做声学侦察后再定")

    result["verdict"] = v
    result["unique_ratio"] = round(unique_ratio, 3)
    print(f"  {v}")
    for r in result["reasons"]:
        print(f"   - {r}")
    print("\n  请与 scripts/audio_probe.py 的声学结论交叉印证后再下结论。")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=1)
        print(f"  → {args.json_out}")


if __name__ == "__main__":
    main()
