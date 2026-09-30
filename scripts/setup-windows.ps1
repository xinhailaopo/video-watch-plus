<#
setup-windows.ps1 —— 在 Windows 上把 video-watch 的依赖一次装齐。

它做的事（按顺序，每步都会校验）
  1. 下载 ffmpeg 完整构建（BtbN win64-gpl），并**逐个校验 7 个必需滤镜**是否都在
  2. 下载 ImageMagick 7 便携版（官方已改发 .7z，用 7zr.exe 解包）
  3. 写用户级 vw.config.json（这才是关键，见下方说明）
  4. 写用户级环境变量与 PATH
  5. 跑 vw.py doctor 复核

为什么必须写配置文件而不只是环境变量
------------------------------------
把变量写进注册表只对**之后新启动**的进程生效；已经在跑的宿主（比如 DSH 本身）
继承的是它启动时的旧环境块，看不到你刚写的变量。配置文件是每次调用现读的，
所以它才能让当前进程立刻可用。实测过：只设环境变量时 doctor 仍报缺项。

用法
----
  # 默认装到 %USERPROFILE%\.video-watch\tools
  powershell -ExecutionPolicy Bypass -File setup-windows.ps1

  # 指定目录 + 指定上游 vw.py 位置（用于写配置与自检）
  powershell -ExecutionPolicy Bypass -File setup-windows.ps1 `
      -ToolsDir D:\tools -VwPy D:\video-watch\vendor\vw.py

  # 只写配置、不下载（依赖已装好时用）
  powershell -ExecutionPolicy Bypass -File setup-windows.ps1 -SkipDownload `
      -Ffmpeg D:\ffmpeg\bin\ffmpeg.exe -Magick D:\ImageMagick\magick.exe
#>
[CmdletBinding()]
param(
    [string]$ToolsDir = (Join-Path $env:USERPROFILE '.video-watch\tools'),
    [string]$Font = 'C:\Windows\Fonts\consola.ttf',
    [string]$VwPy = '',
    [string]$Ffmpeg = '',
    [string]$Magick = '',
    [switch]$SkipDownload
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# 上游 ffmpeg 的 7 个必需滤镜。缺任何一个，probe / grid / seq 都会在运行时报错，
# 而且报错位置离真正原因很远，所以这里提前跑一次 `ffmpeg -filters` 逐项确认。
$RequiredFilters = @('scdet', 'freezedetect', 'silencedetect', 'tblend', 'signalstats', 'tile', 'drawtext')

function Info($m) { Write-Host "[*] $m" }
function Ok($m)   { Write-Host "[ok] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[!] $m" -ForegroundColor Yellow }

function Get-WithRetry([string]$Url, [string]$OutFile, [int]$Tries = 3) {
    for ($i = 1; $i -le $Tries; $i++) {
        try { Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing -TimeoutSec 1800; return }
        catch { if ($i -eq $Tries) { throw } ; Warn "下载失败，重试 $i/$Tries：$($_.Exception.Message)"; Start-Sleep 2 }
    }
}

function Test-FfmpegFilters([string]$Exe) {
    $out = (& $Exe -hide_banner -filters 2>&1) -join "`n"
    $missing = @()
    foreach ($f in $RequiredFilters) {
        if ($out -notmatch "(?m)^\s*\S{1,3}\s+$f\s") { $missing += $f }
    }
    return $missing
}

New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null
Info "工具目录：$ToolsDir"

# --------------------------------------------------------------------------- #
# 1) ffmpeg
# --------------------------------------------------------------------------- #
if (-not $SkipDownload -and -not $Ffmpeg) {
    $zip = Join-Path $ToolsDir 'ffmpeg.zip'
    if (-not (Get-ChildItem $ToolsDir -Directory -Filter 'ffmpeg-*' -ErrorAction SilentlyContinue)) {
        $url = 'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip'
        Info "下载 ffmpeg 完整构建（约 190MB）…"
        Get-WithRetry $url $zip
        Info "解包…"
        Expand-Archive -Path $zip -DestinationPath $ToolsDir -Force
        Remove-Item $zip -Force
    }
    $root = Get-ChildItem $ToolsDir -Directory -Filter 'ffmpeg-*' | Select-Object -First 1
    if ($root) { $Ffmpeg = Join-Path $root.FullName 'bin\ffmpeg.exe' }
}

if ($Ffmpeg -and (Test-Path $Ffmpeg)) {
    $missing = Test-FfmpegFilters $Ffmpeg
    if ($missing.Count -eq 0) { Ok "ffmpeg 七个必需滤镜齐备：$($RequiredFilters -join ', ')" }
    else { Warn "ffmpeg 缺少滤镜：$($missing -join ', ')（probe/grid 会失败，请换用完整构建）" }
    $libs = (& $Ffmpeg -version 2>&1) -join "`n"
    if ($libs -match 'enable-libfreetype') { Ok 'ffmpeg 已启用 libfreetype（drawtext 可用）' }
    else { Warn 'ffmpeg 未启用 libfreetype —— 图版将烧不上帧号索引' }
} else { Warn '未找到 ffmpeg' }

# --------------------------------------------------------------------------- #
# 2) ImageMagick 7（拼贴图版必需）
# --------------------------------------------------------------------------- #
if (-not $SkipDownload -and -not $Magick) {
    $imDir = Join-Path $ToolsDir 'ImageMagick'
    New-Item -ItemType Directory -Force -Path $imDir | Out-Null
    if (-not (Test-Path (Join-Path $imDir 'magick.exe'))) {
        # 官方 portable 版现在是 GitHub Release 上的 .7z；py7zr 解不了它的 BCJ2 过滤器，
        # 所以先用官方独立版 7zr.exe（仅支持 .7z，588KB，免安装）。
        $szr = Join-Path $ToolsDir '7zr.exe'
        if (-not (Test-Path $szr)) {
            Info '下载 7zr.exe…'
            Get-WithRetry 'https://www.7-zip.org/a/7zr.exe' $szr
        }
        $ver = '7.1.2-32'
        $url = "https://github.com/ImageMagick/ImageMagick/releases/download/$ver/ImageMagick-$ver-portable-Q16-x64.7z"
        $seven = Join-Path $ToolsDir 'im.7z'
        Info "下载 ImageMagick $ver portable…"
        Get-WithRetry $url $seven
        Info '解包（需要一两分钟）…'
        & $szr x $seven "-o$imDir" -y | Out-Null
        Remove-Item $seven -Force
    }
    $Magick = Join-Path $imDir 'magick.exe'
}

if ($Magick -and (Test-Path $Magick)) {
    $v = (& $Magick -version 2>$null | Select-Object -First 1)
    Ok "ImageMagick：$v"
    # 注意：IM7 的 `magick convert` 入口在部分构建里不被识别（会被当成文件名），
    # vendor/vw.py 已针对这点做了退化处理，见 patches/vw-windows-fixes.patch。
    # 探测时必须把 stderr 丢掉，并临时把 $ErrorActionPreference 降级：
    # 测"不识别"的那一项本来就会往 stderr 写错误，而 PS 5.1 在 'Stop' 偏好下
    # 会把 native 命令的 stderr 升级成终止错误，从而中断整个脚本。
    $subs = @()
    $savedEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    foreach ($s in @('montage', 'identify', 'convert')) {
        $o = (& $Magick $s -version 2>$null | Select-Object -First 1)
        if ($o -match 'Version:') { $subs += "$s=识别" } else { $subs += "$s=不识别" }
    }
    $ErrorActionPreference = $savedEap
    Info ("magick 子命令：{0}" -f ($subs -join '  '))
} else { Warn '未找到 ImageMagick（grid/seq/sheet 会失败）' }

# --------------------------------------------------------------------------- #
# 3) 用户级配置（关键步骤）
# --------------------------------------------------------------------------- #
$cfgDir = Join-Path $env:USERPROFILE '.config\video-watch'
New-Item -ItemType Directory -Force -Path $cfgDir | Out-Null
$cfgPath = Join-Path $cfgDir 'vw.config.json'

# 注意：PowerShell 5.1 不支持把 `if` 当表达式直接写进哈希表的值里（PS7 才可以）。
# 先把值算好再组表，否则脚本会在解析阶段就报"意外的标记 }"。
$ffmpegVal = ''
if ($Ffmpeg) { $ffmpegVal = $Ffmpeg }
$ffprobeVal = ''
if ($Ffmpeg) {
    $cand = Join-Path (Split-Path $Ffmpeg) 'ffprobe.exe'
    if (Test-Path $cand) { $ffprobeVal = $cand }
}
$magickVal = ''
if ($Magick) { $magickVal = $Magick }

$cfg = [ordered]@{
    ffmpeg  = $ffmpegVal
    ffprobe = $ffprobeVal
    magick  = $magickVal
    font    = $Font
    outdir  = ''
    defaults = [ordered]@{ skeleton = 16; max = 36; cols = 5; panel = 'medium' }
}
[System.IO.File]::WriteAllText($cfgPath, ($cfg | ConvertTo-Json -Depth 4),
    (New-Object System.Text.UTF8Encoding($false)))
Ok "已写用户级配置：$cfgPath"

# 环境变量：给"之后新启动"的 shell 用
foreach ($kv in @(@('VW_FFMPEG', $ffmpegVal), @('VW_FFPROBE', $ffprobeVal), @('VW_MAGICK', $magickVal), @('VW_FONT', $Font))) {
    if ($kv[1]) { [Environment]::SetEnvironmentVariable($kv[0], $kv[1], 'User') }
}
# ASR 相关的两个变量：国内环境必须（直连 HF 超时；Xet 域名不可达会报 401）
[Environment]::SetEnvironmentVariable('HF_ENDPOINT', 'https://hf-mirror.com', 'User')
[Environment]::SetEnvironmentVariable('HF_HUB_DISABLE_XET', '1', 'User')
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
foreach ($p in @((Split-Path $Ffmpeg -ErrorAction SilentlyContinue), (Split-Path $Magick -ErrorAction SilentlyContinue))) {
    if ($p -and $userPath -notlike "*$p*") { $userPath = "$userPath;$p" }
}
[Environment]::SetEnvironmentVariable('Path', $userPath, 'User')
Ok '已写用户级环境变量（VW_* / HF_*）与 PATH'

# --------------------------------------------------------------------------- #
# 4) 自检
# --------------------------------------------------------------------------- #
if ($VwPy -and (Test-Path $VwPy)) {
    Info '运行 vw.py doctor …'
    $env:VW_FFMPEG = $Ffmpeg; $env:VW_FFPROBE = $ffprobe; $env:VW_MAGICK = $Magick; $env:VW_FONT = $Font
    python $VwPy doctor
} else {
    Warn "未指定 -VwPy，跳过 doctor。装完后请手动跑：python vendor\vw.py doctor"
}

Write-Host ''
Ok '完成。下一步：'
Write-Host '   python vendor\vw.py doctor                                   # 复核必需项'
Write-Host '   python scripts\build_testclip.py out\                          # 造带真值的验收素材'
Write-Host '   python vendor\vw.py probe out\clip.mp4 --out out\work          # 跑量测'
Write-Host '   python scripts\audio_probe.py out\clip.mp4                     # 声学侦察'
