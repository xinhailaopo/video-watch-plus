---
name: "video-watch-plus"
description: "看视频与听声音：把视频/GIF/录屏同时翻译成「可核算的图版序列」与「可信度经过检验的音轨结论」，产出带时间戳的观看交付。视觉侧 ffmpeg 逐帧量测（切点/冻结/运动/像素突变/静音）→ 骨架优先采样 → 图版与位置映射 → 画面文字裁切放大读取；听觉侧先做声学侦察判定有无语音，再做 ASR 转写并做幻觉证伪；最后三路证据互校。含 Windows 一键环境安装与上游 vw.py 的两处 Windows 缺陷修补。Invoke when 用户要求看视频/看动图/分析录屏/GIF 拆帧/视频里发生了什么/某时刻画面是什么/把视频里的文字或语音转写出来/这条音轨里有没有人说话。"
---

# video-watch-plus — 看视频与听声音

把连续时间轴同时翻译成两份**可核算**的东西：一份是**离散图版序列**（给只有单帧视觉能力的模型），
一份是**经过检验的音轨结论**（语音 / 音乐 / 静音，以及可信的转写）。

**核心机理**：ffmpeg 看全部帧（零 token），我看其中几十帧，二者靠**时间戳**缝合；
声学层用纯物理量先判"有没有人声"，再让 ASR 回答"说了什么"。

**交付目标**：给出**人能用的观看结论**（完整描述 + 细节 + 带时间节点的转写），不是抖动审计。

本文件写给**执行这个技能的智能体**：下文"我"即执行者。

---

## 两条铁律（违反其一，结论就会是编的）

### 铁律一：图版里的文字一律不采信

实测两次：3×3 图版里 16px 的时间码我"读"出了九个不同值（回原图裁切放大后发现九格字面全同）；
另一次我"读"出一组结构规整的标题，放大后发现画面上根本没有这些字。

> **视觉在分辨率不足时不报"读不到"，它渲染一个合理内容。**

### 铁律二：音频域同样会幻觉，而且形态更隐蔽

实测：对一段**纯 BGM** 视频关掉 VAD 强制出字，Whisper 对三个**互不重叠**的时间窗输出了
**完全相同**的伪造字幕，而模型自报的 `no_speech` 概率高达 0.44–0.74。

> **没有语音时，ASR 会拿最常见的模板句填空。转写文本非空 ≠ 有语音。**

判定规则：**只要发现互不重叠的时间窗输出了相同文本，无论多通顺，一律判为幻觉。**

---

## 环境

```powershell
# Windows 一键装（ffmpeg 完整构建 + ImageMagick 7 便携版 + 用户级配置 + 环境变量）
powershell -ExecutionPolicy Bypass -File scripts\setup-windows.ps1 -VwPy vendor\vw.py
python vendor\vw.py doctor
```

macOS / Linux：`brew install ffmpeg imagemagick` 或 `apt install ffmpeg imagemagick`，
再把 `vendor/vw.py`、`scripts/audio_probe.py` 的 ffmpeg 路径指过去即可。

音频侧可选依赖：

```bash
pip install faster-whisper "av<19" numpy
```

> **`av<19` 不能省。** faster-whisper 1.2.1 调用 `av.open(..., metadata_errors=...)`，
> 而 PyAV 19 已移除该参数，装最新的 av 会在转写时直接 `TypeError`。
> 国内环境还需 `HF_ENDPOINT=https://hf-mirror.com` 与 `HF_HUB_DISABLE_XET=1`
> （后者是因为 huggingface_hub 默认走 Xet 传输，其域名不可达会报 401）。setup 脚本已写好这两个变量。

---

## 工作流

### 视觉侧

```bash
# 1) 量测全片（零 token，先看结构）——切点/冻结/静音/逐秒运动/内容判型
python vendor/vw.py probe 素材.mp4 --out work

# 2) 采样计划（骨架保证盲区有上界）
python vendor/vw.py plan work/timeline.json

# 3) 一张图看全片；随后我用 read_image 看它
python vendor/vw.py grid --media 素材.mp4 --frames 25 --cols 5 --out work

# 4) 读运动（按帧号排序的区域序列）
python vendor/vw.py seq --media 素材.mp4 --times "3.2,3.7,4.2" --out work

# 5) 读字（小字必须裁切放大，这是唯一可信的读字通道）
python vendor/vw.py read --media 素材.mp4 --times "20,45" --region "60,390,1170,570" --out work

# 6) 交付骨架
python vendor/vw.py report work/timeline.json work/shotlist.json --manifest work/manifest.json --out work/report.md
```

档位选择：`grid` 是"要么看全，要么看清"的单向兑换——25 帧铺 5×5 看结构，连续帧铺 6×6 读动作链。

### 听觉侧

```bash
# 7) 声学侦察：先弄清是语音、音乐、纯音还是静音（不依赖语言模型）
python scripts/audio_probe.py 素材.mp4 --json work/audio_probe.json

# 8) 转写 + 幻觉证伪（同时给"开 VAD"与"强制出字"两组结果）
python scripts/asr_hallucination_check.py 素材.mp4 --model small --lang zh
```

**顺序不能颠倒。** 先 7 后 8：如果 7 已经判为"器乐/音乐为主，未见人声特征"，
8 的输出就必须按幻觉处理，不能直接当台词用。

---

## 三路互校（本技能的核心方法）

拿到结论前，把三路证据摆在一起对齐：

| 路 | 来源 | 权威范围 |
|---|---|---|
| **量测** | `vw.py probe`（逐帧，零 token） | **时间**：切点、冻结、静音、运动曲线。先后顺序只信它 |
| **画面文字** | 全分辨率裁切放大后我亲自看 | **文字内容**：字幕、界面文案、文件路径 |
| **音轨** | `audio_probe` + `asr` | **说了什么**；但必须先用声学判"有没有人声" |

对齐规则：

1. **时间只信 manifest**，绝不让视觉印象决定先后。
2. **文字冲突时以画面为准。** 实测：ASR 把画面字幕「大肥鱼」听成「大飞鱼」、
   把「这用户怎么有股味儿啊」整句听错——6 句里错了 3 句。**有画面时画面权威。**
3. **音频判型冲突时以声学量为准**，不以转写文本是否非空为准。
4. 三路都拿不到证据的区段，**明确写"我没有可读信息"**，不要用推断填空。

### 一个可直接复用的独立校验

给合成素材（`scripts/build_testclip.py`）内嵌的画面里烧入了源内时间码，
于是"工具声称的取样时刻"可以和"画面自证的时刻"逐帧对账：

```
seq 条带内源画面显示 00:00.208 / 00:00.708
   → 绝对时刻 3.208s / 3.708s（该段起点 3.0s）
manifest 声称取样于 3.2s / 3.7s
   → 逐帧吻合 ✅
```

**任何一次交付都值得做一次这种"自证对账"**——它比重复跑一遍更能发现时间戳映射错误。

---

## 已知边界（不得越界承诺）

- 运动靠**按帧号排序的帧序列**读；真限制是**时间混叠**（间隔 4 帧 ≈ 只能看清 0.1–0.5s 级动作）
- 小字必须裁切放大；**整帧或图版里直接"读出"的文字可能是编的**
- 「全片覆盖」与「看清每个字」物理上不可兼得（756×756 一张图约 346 token，单图上限 384）
- 声学侦察**只回答"像不像人声"**，不回答"说了什么"；它也不能识别音乐曲名或说话人
- ASR 的 OCR/ASR 类输出都是机器产物，同音错字常见
- 浏览器实时观看是**另一条路**：能拿到弹幕/标签/评论等语境层，但**采样粗且拿不到音频**。
  需要精确取证时仍应落盘。

---

## 与 browser-skill 配合：看现场 + 做取证

本技能与 [`@wxg-prc-cpg/browser-skill-dsh-plugin`](https://www.npmjs.com/package/@wxg-prc-cpg/browser-skill-dsh-plugin)
（底层 [Tencent/BrowserSkill](https://github.com/Tencent/BrowserSkill)）是**互补**关系，不是替代：

| | browser-skill（实时） | 本技能（落盘） |
|---|---|---|
| 下载 | 不需要 | 需要 |
| 取帧 | 按调用节奏采样，间隔不匀 | 任意时刻精确取帧 + 逐帧量测 |
| **声音** | ❌ **拿不到**（截图采不到音频） | ✅ 声学侦察 + ASR |
| 读小字 | 受截图分辨率限制 | 裁切放大（唯一可信通道） |
| **独有能力** | **弹幕/标签/评论/相关推荐/登录态/可交互** | 可复现、可核算的取证交付 |

> **落盘路回答"发生了什么"；实时路回答"这件事在什么语境里"。**

**什么时候该切到实时路**：量测或转写都拿到了、但结论里出现"我不知道为什么"的时候。
实测一次：画面与音轨都只显示"角色化作星点消失"，**为什么消失**只存在于一条弹幕里；
画面的"蓝发→橙发"具体在指代什么，也是页面标签点明的。

**caveat**：browser-skill 的链路是 `bsk` CLI + 本地 daemon + 浏览器扩展 + dsh 插件四件套，
每台机器都要配一次，且有若干只有踩过才知道的坑（PATH 归属、daemon 无法从沙箱进程树分离、
默认 10 分钟空闲自退、浏览器没开导致 `0 browsers connected`）。
完整清单见 [`references/with-browser-skill.md`](references/with-browser-skill.md)。

---

## 关于安装：请让 agent 来做

本技能的安装路径上有一堆**只有踩过才知道**的细节（PATH 究竟是谁的 PATH、
`av<19` 的版本区间、HF 镜像与 Xet 传输、ffmpeg 必须验 7 个滤镜……）。
照着文档一处处试的成本远高于把仓库直接交给 agent：

> 「按 `SKILL.md` 把环境装好，装完跑 `vendor/vw.py doctor` 和 `scripts/build_testclip.py` 验收，
> 把踩到的坑补进 `references/`」

理由是 agent 能读源码验证（本仓库两处 Windows 缺陷就是读 `escape_filter_path()` 源码
+ 写对照实验定位的）、能在失败处继续挖、并且能把过程固化成 `scripts/setup-windows.ps1` 这种可复现脚本。

**必须由人做的只有一件事**：浏览器扩展的安装与授权（browser-skill 上游明确要求用户本人完成）。

> ⚠️ `patches/vw-windows-fixes.patch` 修的两处缺陷是**环境相关**的 ——
> ffmpeg 版本/构建、ImageMagick 是 dll 版还是 portable 版、系统区域设置，都会影响是否触发。
> 先按 `references/windows-setup.md` 里的自查命令确认**你这台机器的表现属于哪一种**，再决定打不打补丁。

---

## 本仓库相对上游的改动

本技能是 [`CFITCorporation/video-watch-skill`](https://github.com/CFITCorporation/video-watch-skill)（MIT，© 2026 CFITSec）
的衍生作品。`vendor/` 下的 `vw.py` 是上游代码**加上两处 Windows 缺陷修补**，其余为新增。

| 新增 | 说明 |
|---|---|
| `scripts/audio_probe.py` | 声学侦察。**新增能力**：整条链路原本只有 ASR，而 ASR 在无语音音轨上会幻觉 |
| `scripts/asr_hallucination_check.py` | 幻觉证伪（开/关 VAD 对照 + 重复模板句检测） |
| `scripts/build_testclip.py` | 生成**带已知基准真值**的验收素材 |
| `scripts/setup-windows.ps1` | Windows 一键环境（含 7 个必需滤镜的逐项校验） |
| `patches/vw-windows-fixes.patch` | 上游 `vw.py` 的两处 Windows 缺陷修补（**环境相关**，详见 `references/windows-setup.md` 开头的自查说明） |
| `references/verification.md` | 三路互校 playbook 与两类幻觉的完整记录 |
| `references/with-browser-skill.md` | 与 browser-skill 配合：分工表、8 步工作流、启用前提与四个实测坑 |
| `verification/` | 带真值对照的实测记录 |

详细用法与踩坑记录见 `references/`；上游工具的完整手册见 `vendor/` 内上游自带的 `SKILL.md` 说明（本文件已覆盖必要部分）。
