# video-watch-plus

**让你的 Agent 既看得见视频，也听得见视频里的声音 —— 并且知道什么时候它其实是在编。**

[English](README.en.md) | **中文**

---

## 它解决什么

让模型"看视频"通常有两种便宜做法，各有毛病：

- **均匀抽几帧** —— 快速动作整段漏掉，抽几帧全凭手感，还说不出覆盖有没有盲区；
- **逐帧喂进去** —— token 爆炸，而绝大多数帧是重复信息。

而"听视频"更麻烦：**ASR 在没有语音的音轨上不会说"我没听到人声"，它会编一句出来。**

本技能把这两件事一起做掉：

> **ffmpeg 看全部帧（零 token）+ 声学量测先判有无语音，模型只负责看几十帧和命名。**

一句话判据：**量测给候选，眼睛给命名；声学判有无，转写给内容。**

---

## 三件它比"抽帧给模型看"多做的事

### 1. 声学侦察：先证明"到底有没有人声"

`scripts/audio_probe.py` 只用 numpy，从波形与频谱算出可解释的物理量，给出判型：

| 判据 | 音乐 | 语音 | 纯音/提示音 |
|---|---|---|---|
| 起始包络自相关峰（节拍） | **高**（实测 0.86） | 低（0.17） | 极低（0.001） |
| 谱平坦度 | **极低**（0.0001） | 中（0.023） | 极低（0.00003） |
| 逐秒响度波动 | **小**（6.3 dB） | 大（19.1 dB） | 极大 |
| 单音集中度 | 低 | 低 | **高**（0.58） |

它**不做识别**，只做量测——和 `probe` 是同一个设计哲学。结论例如：

```
判型 器乐/音乐为主，未见人声特征（置信 high）
  - 节拍自相关峰 0.86 @ 117 BPM（有稳定节拍）
  - 谱平坦度 0.0001（强谐波结构，噪声趋近 1）
  - 逐秒响度波动仅 6.3 dB（像连续音乐床，缺少语音起停）
  - 85% 谱滚降只有 466 Hz（几乎无高频瞬态，连键鼠点击都没有）
```

### 2. 幻觉证伪：一条音轨的转写是"听到了"还是"编出来的"

`scripts/asr_hallucination_check.py` 同时跑"开 VAD"与"拆掉所有拒识门槛强制出字"两组，
用**互不重叠的时间窗是否输出完全相同文本**来判定。真实例：

```
== 实验组：关闭 VAD + 拆掉所有拒识门槛（强制出字）==
  [    0.00 →     2.00] no_speech=0.440 | '字幕by索兰娅'
  [   30.00 →    48.00] no_speech=0.735 | '字幕by索兰娅'

== 判定 ==
  判定为幻觉：音轨很可能没有人声
   - 2 个互不重叠的时间窗输出了完全相同的文本 '字幕by索兰娅'
   - 模型自报 no_speech 概率均值 0.59（它自己也不认为这里有人说话）
```

同一段工具用在真有人声的素材上，则给出：

```
  转写可信：有真实语音
   - VAD 模式得到 6 段；强制模式 6 段中 6 段文本各不相同（unique 比 1.00）
     ——幻觉不会产出这种多样性
   - 模型自报 no_speech 概率均值仅 0.04
```

### 3. 三路互校：量测 / 画面文字 / 音轨，谁说了算

| 路 | 权威范围 |
|---|---|
| 量测（`probe`，逐帧零 token） | **时间**：切点、冻结、静音、运动曲线 |
| 画面文字（全分辨率裁切放大后亲眼看） | **文字内容** |
| 音轨（`audio_probe` + `asr`） | **说了什么**（但先要过声学那关） |

实测收益：一次真实交付里 ASR 把画面字幕「大肥鱼」听成「大飞鱼」、把「这用户怎么有股味儿啊」
整句听错——**6 句错了 3 句**。全部靠"文字冲突以画面为准"这条规则纠正回来。

---

## 快速开始

```powershell
# 1) 装环境（Windows：ffmpeg 完整构建 + ImageMagick 7 + 用户级配置）
powershell -ExecutionPolicy Bypass -File scripts\setup-windows.ps1 -VwPy vendor\vw.py
python vendor\vw.py doctor

# 2) 造一段"知道正确答案"的素材（切点/冻结/静音全部人为设定）
python scripts\build_testclip.py out\

# 3) 量测 + 视觉
python vendor\vw.py probe out\clip.mp4 --out out\work
python vendor\vw.py grid  --media out\clip.mp4 --frames 25 --cols 5 --out out\work
#    ← 然后我用 read_image 看这张图版，并按铁律对文字裁切放大再读

# 4) 声学 + 转写
python scripts\audio_probe.py out\clip.mp4
python scripts\asr_hallucination_check.py out\clip.mp4 --model small --lang zh
```

音频侧可选依赖：

```bash
pip install faster-whisper "av<19" numpy
```

> **`av<19` 不能省**：faster-whisper 1.2.1 用了 PyAV 19 已移除的 `metadata_errors` 参数，
> 装最新 av 会在转写时直接 `TypeError`。国内还需 `HF_ENDPOINT=https://hf-mirror.com`
> 与 `HF_HUB_DISABLE_XET=1`（后者：huggingface_hub 默认走 Xet 传输，域名不可达会报 401）。

---

## 它为什么在 Windows 上能跑

上游工具在 Windows 上有两处会让 `grid` / `seq` / `sheet` / `read` **直接失败**的缺陷，
本仓库用实测对照表定位并修补（详见 [`references/windows-setup.md`](references/windows-setup.md)）：

| 缺陷 | 实测 | 修法 |
|---|---|---|
| ffmpeg 的 `drawtext` 吃不下盘符路径 | `fontfile=C:/x.ttf` ❌ · `fontfile=C\:/x.ttf` ❌ ← **上游写法** · `fontfile='C\:/x.ttf'` ✅ | 转义后整体加单引号 |
| ImageMagick 7 的 `magick convert` 入口 | 本构建下 `convert` 是**唯一**不被识别的子命令（被当成输入文件名 → `no decode delegate for 'convert'`）；`montage`/`identify` 等均正常 | 7.x 下 `convert` 退化为裸入口 |

补丁见 [`patches/vw-windows-fixes.patch`](patches/vw-windows-fixes.patch)。

---

## 仓库结构

```
SKILL.md                        技能本体（给 Agent 读的操作手册）
README.md / README.en.md        本文件
LICENSE                         MIT（本项目部分）
NOTICE                          上游归属与衍生说明
patches/
  vw-windows-fixes.patch        对上游 vw.py 的两处修补
scripts/
  audio_probe.py                声学侦察（新增）
  asr_hallucination_check.py    ASR 幻觉证伪（新增）
  build_testclip.py             生成带已知真值的验收素材（新增）
  setup-windows.ps1             Windows 一键环境（新增）
references/
  windows-setup.md              Windows 依赖坑的完整解法
  verification.md               三路互校 playbook 与两类幻觉
verification/
  record-*.md                   带真值对照的实测记录
vendor/                         上游 MIT 代码（vw.py 已打补丁 + vwtools.py + 上游 LICENSE）
```

---

## 已知边界（读之前请先读这一段）

- **声学侦察不识别内容**：它只回答"像不像人声"，不回答"说了什么"，也不能识别曲名或说话人。
- **ASR 有同音错字**，有画面时以画面为准。
- **图版里的文字一律不采信**，必须裁切放大。
- **运动靠按帧号排序的帧序列读**；间隔 4 帧只能看清 0.1–0.5s 级动作（时间混叠）。
- 「全片覆盖」与「看清每个字」物理上不可兼得。
- 本技能只做"看懂"，**不做剪辑**。要剪视频是另一类工具的事。

---

## 许可

本项目为 MIT。`vendor/` 内包含上游 [`CFITCorporation/video-watch-skill`](https://github.com/CFITCorporation/video-watch-skill)
代码（MIT，Copyright © 2026 CFITSec），版权归属与衍生关系见 [NOTICE](NOTICE)。
