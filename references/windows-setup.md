# Windows 环境：必须知道的坑（附实测对照）

本文件记录在 Windows 上把这条链路跑通所踩过的坑，每条都有**实测对照**而非推测。
按顺序照做，或用 `scripts/setup-windows.ps1` 一键完成。

> ## ⚠️ 先读这一条：这些都是**环境相关**的表现，不是"人人必现"
>
> 本文件里的"坑"来自**特定环境**的实测：
>
> ```
> Windows 11
> ffmpeg N-126965-gd85cdd2597-20260929（win64-gpl 完整构建）
> ImageMagick 7.1.2-32 Q16 x64 portable
> Python 3.13.14        系统区域：中文（GBK 代码页）
> ```
>
> 换一个 ffmpeg 构建、换成 ImageMagick 的 dll 版、或把系统区域改成英文，
> **表现都可能不一样**。比如：
>
> - `drawtext` 的路径解析在不同 libfreetype/libfontconfig 构建之间并不一致；
> - `magick convert` 的兼容入口在 6.x 与不同 7.x 小版本之间也有差异；
> - `escape_filter_path()` 那种"多字节错位吃掉引号"的现象，**只在非 UTF-8 代码页上才出现**
>   （英文系统按 CP1252 读，结果又不相同）。
>
> **所以正确用法是**：把下面的**自查命令**先在自己的机器上跑一遍，看你的环境属于哪一种表现，
> 再决定要不要动手。不要因为文档里写了"❌ 失败"就认定自己一定也会失败。
>
> 各条自查命令都写在该坑的正文里；最省事的整体自查是：
>
> ```powershell
> python vendor\vw.py doctor                              # 必需项是否齐备
> ffmpeg -hide_banner -filters | Select-String '\b(scdet|freezedetect|silencedetect|tblend|signalstats|tile|drawtext)\b'
> ffmpeg -version | Select-String 'enable-libfreetype'
> magick montage -version                                # 以及 identify / convert 各自试一次
> ```

---

## 坑 1：ffmpeg 必须是完整构建，且要逐项验滤镜

上游工具依赖 7 个滤镜，缺任何一个都会在**运行中途**报错，而且报错位置离真正原因很远：

```
scdet  freezedetect  silencedetect  tblend  signalstats  tile  drawtext
```

自检方法（安装脚本里也是这么做的）：

```powershell
ffmpeg -hide_banner -filters | Select-String -Pattern '\b(scdet|freezedetect|silencedetect|tblend|signalstats|tile|drawtext)\b'
```

另外 **`drawtext` 需要 ffmpeg 编译时带 libfreetype**，部分发行版与精简构建不带。
确认方式：

```powershell
ffmpeg -version | Select-String 'enable-libfreetype'
```

实测可用的构建：`BtbN/FFmpeg-Builds` 的 `ffmpeg-master-latest-win64-gpl.zip`
（写这份记录时其 `-filters` 输出里七个滤镜齐全，且 `enable-libfreetype` / `enable-libfontconfig` 均已启用）。

---

## 坑 2：`drawtext` 吃不下盘符路径 —— 上游 `escape_filter_path()` 在 Windows 上失效

**症状**：`grid` / `seq` / `read` 直接失败，报

```
No option name near '/Windows/Fonts/consola.ttf:text=1:x=4:y=2:fontsize=18:...'
```

**根因**：上游只做 `path.replace(":", "\\:")`，产出 `fontfile=C\:/Windows/Fonts/consola.ttf`。

**四组实测对照**（本机 ffmpeg N-126965 win64-gpl，`drawtext`，subprocess 直接传 argv）：

| 写法 | 结果 |
|---|---|
| `fontfile=C:/x.ttf` | ❌ 失败 |
| `fontfile=C\:/x.ttf` ← **上游当前写法** | ❌ 失败 |
| `fontfile='C\:/x.ttf'` | ✅ 通过 |
| `fontfile=C\\:/x.ttf` | ✅ 通过（双反斜杠） |
| `fontfile=consola.ttf`（相对路径，无冒号） | ✅ 通过 |

**修法**：Windows 上给转义后的路径**整体加单引号**。见 `patches/vw-windows-fixes.patch` 第一处。

```python
def escape_filter_path(path):
    p = path.replace("\\", "/").replace(":", "\\:")
    if os.name == "nt":
        return "'" + p + "'"     # 关键：必须有引号
    return p
```

---

## 坑 3：ImageMagick 7 的 `magick convert` 入口在便携构建里不被识别

**症状**：`seq` / `sheet` / `--diff` 报

```
magick.exe: no decode delegate for this image format `convert'
```

**根因**：上游对 IM 7.x 统一拼 `[exe, sub]`，即 `magick convert ...`。
但在这个构建里 `convert` 被当作**输入文件名**。

**逐个子命令实测**：

| 子命令 | 是否被识别 |
|---|---|
| `montage` | ✅ |
| `identify` | ✅ |
| `compare` | ✅ |
| `composite` | ✅ |
| `mogrify` | ✅ |
| `stream` | ✅ |
| `conjure` | ✅ |
| **`convert`** | ❌ **唯一不被识别的** |

所以 `grid` 因为走 `montage` 而侥幸可用，而 `seq` / `sheet` 必挂。

**修法**：7.x 下 `convert` 退化为裸入口 `magick <in...>`（IM 7 原生写法，功能等价）。
见 `patches/vw-windows-fixes.patch` 第二处。

---

## 坑 4：ImageMagick 便携版已改发 `.7z`，而 py7zr 解不了它

官方下载页现在指向 GitHub Release 上的 **`.7z`**：

```
https://github.com/ImageMagick/ImageMagick/releases/download/7.1.2-32/ImageMagick-7.1.2-32-portable-Q16-x64.7z
```

- `py7zr` 会失败：`UnsupportedCompressionMethodError: BCJ2 filter is not supported by py7zr`
- 系统多半也没有 `7z.exe`，且不方便为此装一整套 7-Zip
- **解法**：用官方**独立版** `7zr.exe`（仅支持 `.7z`，588KB，免安装）
  `https://www.7-zip.org/a/7zr.exe` —— `7zr x im.7z -o<目录> -y`

---

## 配置为什么必须落盘成文件

把变量写进注册表（`SetEnvironmentVariable(..., 'User')`）只对**之后新启动**的进程生效。
已经在跑的宿主（编辑器、Agent 框架、终端）继承的是它启动那一刻的环境块，**看不到新变量**。

实测：只设环境变量时，同一台机器上的 `vw.py doctor` 仍报缺项；写入配置文件后立刻可用。

所以安装脚本两条都做：

- **用户级配置文件**（每次都现读，立即生效）：
  `~/.config/video-watch/vw.config.json`
- **用户级环境变量 + PATH**（给以后新开的 shell 用）：
  `VW_FFMPEG` / `VW_FFPROBE` / `VW_MAGICK` / `VW_FONT`

配置查找顺序（与 `vw.py` 的 `config_candidates()` 一致，`audio_probe.py` 也遵循同一顺序）：

```
--config  >  VW_CONFIG  >  ./vw.config.json  >  ~/.config/video-watch/vw.config.json
```

---

## 音频侧的三个额外坑（可选依赖）

只有用到 ASR 时才会遇到，但每一条都会让转写**直接失败**：

| 现象 | 根因 | 解法 |
|---|---|---|
| `httpx.ConnectTimeout`（卡很久后失败） | 直连 `huggingface.co` 超时 | `HF_ENDPOINT=https://hf-mirror.com` |
| 换镜像后仍 `401 Unauthorized`，域名是 `cas-server.xethub.hf.co` | huggingface_hub 默认走 **Xet** 传输，该域名不可达 | `HF_HUB_DISABLE_XET=1` 退回经典 HTTP |
| `TypeError: open() got an unexpected keyword argument 'metadata_errors'` | faster-whisper 1.2.1 需要该参数，而 **PyAV 19 已移除** | `pip install "av<19"` |
| `Library cublas64_12.dll is not found`（用 GPU 时） | 缺 CUDA/cuDNN 运行库 | 装 `nvidia-cublas-cu12 nvidia-cudnn-cu12`，或直接用 CPU |

> CPU 实测性能：`small` 模型 int8 下约 **9× 实时**（15.55s 音频 1.8s），
> 多数场景不必上 GPU。注意上游 `cmd_asr` 里 `device="cpu"` 是**写死**的。

---

## 坑 5：PowerShell 5.1 读**无 BOM** 的 UTF-8 脚本会按 ANSI 解析

本仓库的 `scripts/setup-windows.ps1` 因此**必须带 UTF-8 BOM**。

**症状**：脚本语法明明正确，`-File` 运行却报一串莫名其妙的解析错误：

```
line 119: 表达式或语句中包含意外的标记"}。"
line 121: 表达式或语句中包含意外的标记"}。"
line 158: 语句块或类型定义中缺少右"}"。
```

而括号平衡检查显示**完全平衡**——因为问题不在括号。

**根因**：Windows PowerShell 5.1 判断脚本编码时，**有 BOM 才按 UTF-8 读，没有 BOM 就按系统 ANSI 代码页读**
（中文系统上是 GBK）。于是脚本里的中文被拆成错误的字节序列，**多字节错位会吃掉字符串的收尾引号**：

```powershell
# 源码（UTF-8）
if ($o -match 'Version:') { $subs += "$s=识别" } else { $subs += "$s=不识别" }

# PS 5.1 按 GBK 误读后
if ($o -match 'Version:') { $subs += "$s=璇嗗埆" } else { $subs += "$s=涓嶈瘑鍒? }
                                                                          ↑ 收尾引号被吃掉
```

字符串没闭合，后面所有 `}` 就都成了"意外的标记"。

**修法**：写脚本时显式带 BOM。

```powershell
$txt = [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8)
[System.IO.File]::WriteAllText($path, $txt, (New-Object System.Text.UTF8Encoding($true)))  # $true = 带 BOM
```

**自查**：

```powershell
$b = [System.IO.File]::ReadAllBytes($path)
$hasBom = ($b[0] -eq 0xEF -and $b[1] -eq 0xBB -and $b[2] -eq 0xBF)
$err = $null
[void][System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$null, [ref]$err)
```

> 顺带一个同源问题：在 `$ErrorActionPreference = 'Stop'` 下，**native 命令写 stderr 会被 PS 5.1 升级成终止错误**。
> 探测"某个命令是否可用"这类代码必然会产生 stderr，必须在探测期间把偏好临时降级为 `'Continue'`，
> 否则脚本会在探测处直接中断。`2>$null` 单独用**不够**。

---

## 一个测试素材层面的坑（非工具问题）

造验收素材时，`drawbox` 放在 `concat` **之前**的话，它的时间基是"段内 0..N 秒"而不是全片，
于是 `x='(t-3)*140'` 恒为负 → 方块被画到画外，视频里根本看不到运动。

另外本机 ffmpeg N-126965 上 **`drawbox` 的表达式型 `x` 不产出图形**（`x=100` 有效、
`x='(t-3)*140'` 无效，可用 `signalstats` 的 `SATAVG` 对照验证）。

要造运动段，直接用 `testsrc2` 这类天然运动源最省事——`scripts/build_testclip.py` 就是这么做的。
