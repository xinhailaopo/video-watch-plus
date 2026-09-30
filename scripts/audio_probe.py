#!/usr/bin/env python3
"""audio_probe.py —— 声学侦察：在信任任何转写之前，先弄清这条音轨里到底有什么。

为什么需要它
------------
ASR（Whisper 系）在**没有语音**的音轨上不会说"我没听到人声"，它会**编一句出来**。
实测（本仓库 verification/ 有记录）：对一段纯 BGM 视频关掉 VAD 强制出字，
三个互不重叠的时间窗输出了**完全相同**的伪造字幕，而模型自报的 no_speech 概率高达 0.44–0.74。

所以判定"有没有人声"不能只看转写文本是否非空，必须有一路**与语言模型无关**的声学证据。
本脚本就是那一路：只用 numpy，从波形与频谱里算出可解释的指标，给出判型。
它不做识别，只做量测——和 vw.py 的 probe 是同一个设计哲学。

判据（都是可复算的物理量，不是感觉）
------------------------------------
  谱平坦度 = 功率谱的几何均值 / 算术均值
      噪声 → 接近 1；乐音、语音这类有谐波结构的声音 → 远小于 1
  起始包络自相关峰值（在 60–200 BPM 对应的延迟范围内取最大）
      音乐有稳定节拍 → 峰很高（实测纯净 BGM 达 0.77–0.86）
      语音没有固定节拍 → 峰低
  逐秒响度曲线的波动
      连续音乐床 → 近乎一条直线；语音有起停 → 波动大
  85% 谱滚降
      过低的滚降（几百 Hz）说明几乎没有高频瞬态 → 连键盘/鼠标/点击声都没有

它**不能**替代 ASR：本脚本只回答"听起来像不像人声"，不回答"说了什么"。
正确流程是 audio_probe（有没有）→ asr（说了什么）→ 互相印证。

用法
----
    python audio_probe.py clip.mp4
    python audio_probe.py clip.mp4 --json out.json
    python audio_probe.py clip.mp4 --ffmpeg D:\\ffmpeg\\bin\\ffmpeg.exe
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import wave

try:
    import numpy as np
except ImportError:  # pragma: no cover
    sys.exit("需要 numpy：pip install numpy")

SR = 16000          # 分析采样率
N = 2048            # FFT 窗长
HOP = 512           # 帧移


# --------------------------------------------------------------------------- #
# 解码
# --------------------------------------------------------------------------- #
def _config_ffmpeg(override: str | None) -> str | None:
    """读与 vw.py 同一份配置，保持整条工具链只有一个配置来源。

    查找顺序（与 vw.py 一致）：--config > VW_CONFIG > ./vw.config.json
    > ~/.config/video-watch/vw.config.json。取其中的 "ffmpeg" 键。
    """
    import json
    cands = [override, os.environ.get("VW_CONFIG"),
             os.path.join(os.getcwd(), "vw.config.json"),
             os.path.join(os.path.expanduser("~"), ".config", "video-watch", "vw.config.json")]
    for c in cands:
        if c and os.path.exists(c):
            try:
                with open(c, encoding="utf-8") as fh:
                    return (json.load(fh) or {}).get("ffmpeg") or None
            except (OSError, ValueError):
                continue
    return None


def resolve_ffmpeg(explicit: str | None, config: str | None = None) -> str:
    for cand in (explicit, os.environ.get("VW_FFMPEG"), _config_ffmpeg(config),
                 shutil.which("ffmpeg")):
        if cand and os.path.exists(cand):
            return cand
    sys.exit("找不到 ffmpeg。按优先级可任选一种指定方式：\n"
             "  1) --ffmpeg <路径>\n"
             "  2) 环境变量 VW_FFMPEG\n"
             "  3) vw.config.json 里的 \"ffmpeg\" 键（与 video-watch 共用）\n"
             "  4) 把 ffmpeg 放进 PATH")


def decode_mono(path: str, ffmpeg: str) -> "np.ndarray":
    """用 ffmpeg 解码成 16k 单声道 s16le，从 stdout 读原始 PCM（不落临时文件）。"""
    cmd = [ffmpeg, "-v", "error", "-i", path, "-vn",
           "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0:
        sys.exit(f"ffmpeg 解码失败：{p.stderr.decode('utf-8', 'replace')[:400]}")
    x = np.frombuffer(p.stdout, dtype="<i2").astype(np.float32) / 32768.0
    if x.size < N * 4:
        sys.exit("音轨太短（不足 4 帧），无法分析")
    return x


# --------------------------------------------------------------------------- #
# 特征
# --------------------------------------------------------------------------- #
def features(x: "np.ndarray") -> dict:
    win = np.hanning(N).astype(np.float32)
    nfr = (x.size - N) // HOP
    freqs = np.fft.rfftfreq(N, 1.0 / SR)

    mags = np.empty((nfr, freqs.size), dtype=np.float32)
    rms = np.empty(nfr, dtype=np.float32)
    zcr = np.empty(nfr, dtype=np.float32)

    for i in range(nfr):
        seg = x[i * HOP:i * HOP + N]
        rms[i] = np.sqrt(np.mean(seg ** 2) + 1e-12)
        mags[i] = np.abs(np.fft.rfft(seg * win))
        zcr[i] = np.mean(np.abs(np.diff(np.sign(seg))) > 0)

    P = mags ** 2 + 1e-12
    total = P.sum(axis=1, keepdims=True)

    db = 20.0 * np.log10(rms + 1e-12)

    # 只在"有声帧"上统计谱指标。
    # 这一点很关键：静音帧的频谱本质是数值噪声，平坦度接近 1，
    # 若把它们算进均值，会把整条音轨的平坦度/质心/滚降全部拉高，导致误判。
    # （本脚本第一版就踩了这个坑：一段"40% 静音 + 纯 440Hz 单音"被算成平坦度 0.40。）
    active = db > -50.0
    if not active.any():
        active = np.ones_like(active)          # 全静音时退化为全帧，由判型分支处理
    Pa, Ta = P[active], total[active, 0]
    freqs_b = freqs

    centroid = (Pa * freqs_b).sum(axis=1) / Ta
    flatness = np.exp(np.log(Pa).mean(axis=1)) / Pa.mean(axis=1)
    cum = np.cumsum(Pa, axis=1) / Ta[:, None]
    rolloff = freqs_b[np.argmax(cum >= 0.85, axis=1)]
    band = (freqs_b >= 300) & (freqs_b <= 3400)
    speech_band = Pa[:, band].sum(axis=1) / Ta
    # 单音集中度：最大谱峰占该帧总能量的比例。纯正弦接近 1，语音/音乐远低。
    tonal = (Pa.max(axis=1) / Ta).mean()

    zcr_a = zcr[active]

    # 起始强度包络（正谱通量），去均值后自相关
    flux = np.maximum(0.0, np.diff(mags, axis=0)).sum(axis=1)
    flux = flux - flux.mean()
    fps = SR / HOP
    ac = np.correlate(flux, flux, "full")[flux.size - 1:]
    ac = ac / (ac[0] + 1e-12) if ac[0] > 0 else ac

    # 节拍：在 60–200 BPM 对应的延迟上取最大峰
    lo = max(1, int(round(fps * 60 / 200)))
    hi = max(lo + 1, int(round(fps * 60 / 60)))
    seg_ac = ac[lo:hi] if hi <= ac.size else ac[lo:]
    lag = lo + int(np.argmax(seg_ac)) if seg_ac.size else 0
    bpm = 60.0 * fps / lag if lag else 0.0
    beat_peak = float(seg_ac.max()) if seg_ac.size else 0.0

    # 逐秒响度波动（语音有起停，连续音乐床几乎是直线）
    per_sec = max(1, int(round(fps)))
    sec_db = np.array([db[i:i + per_sec].mean()
                       for i in range(0, nfr, per_sec)])
    loud_swing = float(np.percentile(sec_db, 95) - np.percentile(sec_db, 5)) if sec_db.size > 2 else 0.0

    # 音节率调制：起始包络在 2–8 Hz 的能量占比（语音偏高，慢节拍音乐偏低）
    env = np.maximum(0.0, np.diff(mags, axis=0)).sum(axis=1)
    env = env - env.mean()
    spec = np.abs(np.fft.rfft(env))
    mf = np.fft.rfftfreq(env.size, 1.0 / fps)
    syl = spec[(mf >= 2) & (mf <= 8)].sum()
    rhythm = spec[(mf >= 0.5) & (mf < 2)].sum()
    syl_ratio = float(syl / (syl + rhythm + 1e-12))

    return {
        "duration_s": round(x.size / SR, 3),
        "frames": int(nfr),
        "frame_rate": round(fps, 2),
        "rms_dbfs_mean": round(float(db.mean()), 2),
        "rms_dbfs_peak": round(float(db.max()), 2),
        "rms_dbfs_min": round(float(db.min()), 2),
        "dynamic_range_db": round(float(db.max() - db.min()), 2),
        "silence_ratio_below_-50dbfs": round(float((db < -50).mean()), 4),
        "active_frame_ratio": round(float(active.mean()), 4),
        "spectral_centroid_hz": round(float(np.mean(centroid)), 1),
        "spectral_flatness": round(float(np.mean(flatness)), 5),
        "rolloff85_hz": round(float(np.mean(rolloff)), 1),
        "speech_band_300_3400_ratio": round(float(np.mean(speech_band)), 4),
        "tonal_concentration": round(float(tonal), 4),
        "zcr_mean": round(float(np.mean(zcr_a)), 5),
        "beat_bpm": round(bpm, 1),
        "beat_autocorr_peak": round(beat_peak, 4),
        "loudness_swing_db_p95_p5": round(loud_swing, 2),
        "syllabic_modulation_2_8hz_ratio": round(syl_ratio, 4),
    }


# --------------------------------------------------------------------------- #
# 判型
# --------------------------------------------------------------------------- #
def verdict(f: dict) -> dict:
    """给出判型与理由。设计上偏向保守：宁可说'不确定'也不硬下结论。"""
    reasons: list[str] = []

    if f["silence_ratio_below_-50dbfs"] > 0.9:
        return {"kind": "静音/近乎无声", "confidence": "high",
                "reasons": ["90% 以上帧低于 -50 dBFS"]}

    # 纯音/提示音：某单一谱峰几乎吃满该帧能量，且毫无节拍起伏。
    # 这类音轨既不是音乐也不是语音，必须单独一支，否则会被含糊地归到"可能含人声"。
    if f["tonal_concentration"] >= 0.25 and f["beat_autocorr_peak"] < 0.3:
        return {"kind": "纯音/提示音（非语音、非音乐）", "confidence": "medium",
                "reasons": [
                    f"单音集中度 {f['tonal_concentration']:.3f}（单一谱峰占该帧大部分能量）",
                    f"节拍自相关峰仅 {f['beat_autocorr_peak']:.3f}（无节奏结构）",
                    f"有声帧占比 {f['active_frame_ratio']*100:.1f}%"]}

    musical = []
    if f["beat_autocorr_peak"] >= 0.5:
        musical.append(f"节拍自相关峰 {f['beat_autocorr_peak']:.2f} @ {f['beat_bpm']:.0f} BPM（有稳定节拍）")
    if f["spectral_flatness"] <= 0.02:
        musical.append(f"谱平坦度 {f['spectral_flatness']:.4f}（强谐波结构，噪声趋近 1）")
    if f["loudness_swing_db_p95_p5"] <= 10:
        musical.append(f"逐秒响度波动仅 {f['loudness_swing_db_p95_p5']:.1f} dB（像连续音乐床，缺少语音起停）")
    if f["rolloff85_hz"] <= 1200:
        musical.append(f"85% 谱滚降只有 {f['rolloff85_hz']:.0f} Hz（几乎无高频瞬态，连键鼠点击都没有）")

    speechless = []
    if f["syllabic_modulation_2_8hz_ratio"] < 0.35:
        speechless.append(f"2–8 Hz 音节率调制占比仅 {f['syllabic_modulation_2_8hz_ratio']:.2f}")

    if len(musical) >= 3:
        return {"kind": "器乐/音乐为主，未见人声特征",
                "confidence": "high" if len(musical) >= 4 else "medium",
                "reasons": musical + speechless}

    if len(musical) == 2:
        return {"kind": "疑似音乐，但证据不足以下定论",
                "confidence": "medium", "reasons": musical}

    return {"kind": "声学量测未见明确人声特征，也未达音乐判据",
            "confidence": "low",
            "reasons": ["未达到音乐判据"] + speechless +
                       [f"单音集中度 {f['tonal_concentration']:.3f}，"
                        f"谱平坦度 {f['spectral_flatness']:.4f}",
                        "此类音轨请交给 ASR，并以 VAD 结果为准"]}


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(
        description="声学侦察：判断音轨里是语音、音乐还是静音（不做识别）")
    ap.add_argument("media", help="视频或音频文件")
    ap.add_argument("--ffmpeg", default=None, help="ffmpeg 路径（默认查 VW_FFMPEG / vw.config.json / PATH）")
    ap.add_argument("--config", default=None, help="vw.config.json 路径（默认按 vw.py 的顺序查找）")
    ap.add_argument("--json", dest="json_out", default=None, help="把结果写入 JSON")
    args = ap.parse_args()

    if not os.path.exists(args.media):
        sys.exit(f"找不到文件 {args.media}")

    ffmpeg = resolve_ffmpeg(args.ffmpeg, args.config)
    x = decode_mono(args.media, ffmpeg)
    f = features(x)
    v = verdict(f)

    print(f"素材 {os.path.basename(args.media)}")
    print(f"时长 {f['duration_s']}s  帧 {f['frames']}  帧率 {f['frame_rate']}/s")
    print(f"响度 均值 {f['rms_dbfs_mean']} dBFS  峰值 {f['rms_dbfs_peak']}  "
          f"动态 {f['dynamic_range_db']} dB  静音占比 {f['silence_ratio_below_-50dbfs']*100:.1f}%")
    print(f"谱   质心 {f['spectral_centroid_hz']} Hz  平坦度 {f['spectral_flatness']}  "
          f"85%滚降 {f['rolloff85_hz']} Hz  语音带占比 {f['speech_band_300_3400_ratio']*100:.1f}%")
    print(f"节拍 {f['beat_bpm']} BPM（自相关峰 {f['beat_autocorr_peak']}）  "
          f"响度波动 {f['loudness_swing_db_p95_p5']} dB  音节率调制 {f['syllabic_modulation_2_8hz_ratio']}")
    print(f"\n判型 {v['kind']}（置信 {v['confidence']}）")
    for r in v["reasons"]:
        print(f"  - {r}")
    print("\n提醒：本脚本只回答'像不像人声'，不回答'说了什么'。"
          "判定有无语音请与 ASR 的 VAD 结果交叉印证。")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump({"media": os.path.basename(args.media), "features": f,
                       "verdict": v}, fh, ensure_ascii=False, indent=1)
        print(f"→ {args.json_out}")


if __name__ == "__main__":
    main()
