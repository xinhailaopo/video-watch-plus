# video-watch-plus

**Give your agent both eyes and ears for video — and a way to tell when it is making things up.**

[中文](README.md) | **English**

---

## The problem

Two cheap ways to let a model "watch" a video, each with its own flaw:

- **Sample a handful of frames** — fast motion is missed entirely, the frame count is a guess,
  and nobody can say whether the coverage has blind spots.
- **Feed in every frame** — token costs explode, and most frames carry no new information.

And "hearing" a video is worse: **an ASR model will not tell you it heard nothing — it invents a line.**

This skill handles both at once:

> **ffmpeg looks at every frame (zero tokens) + acoustic measurement decides whether speech exists at all.
> The model only looks at a few dozen frames and assigns names.**

One-line criterion: **measurement nominates candidates, eyes assign names; acoustics decide presence, ASR supplies content.**

---

## Three things it does that "sample frames and show the model" does not

### 1. Acoustic reconnaissance — prove whether speech exists before trusting any transcript

`scripts/audio_probe.py` uses numpy only, derives interpretable physical quantities from the waveform
and spectrum, and returns a verdict:

| Signal | Music | Speech | Pure tone |
|---|---|---|---|
| Onset-envelope autocorrelation peak (beat) | **high** (0.86 measured) | low (0.17) | ~0 (0.001) |
| Spectral flatness | **very low** (0.0001) | mid (0.023) | very low (3e-05) |
| Per-second loudness swing | **small** (6.3 dB) | large (19.1 dB) | huge |
| Tonal concentration | low | low | **high** (0.58) |

It performs **no recognition** — only measurement, the same design philosophy as `probe`.

### 2. Hallucination falsification — was the transcript heard, or fabricated?

`scripts/asr_hallucination_check.py` runs both "VAD on" and "all rejection thresholds removed,
force output", then decides using **whether disjoint time windows emit identical text**:

```
== forced (VAD off, thresholds removed) ==
  [    0.00 →     2.00] no_speech=0.440 | '字幕by索兰娅'
  [   30.00 →    48.00] no_speech=0.735 | '字幕by索兰娅'

== verdict ==
  hallucination: this track very likely has no speech
   - 2 disjoint windows produced identical text
   - mean model-reported no_speech probability 0.59
```

On a track that really does contain speech, the same tool reports **trustworthy** and explains why
(all 6 segments differ, `no_speech` mean 0.04).

### 3. Three-way reconciliation — measurement / on-screen text / audio track

| Channel | Authoritative for |
|---|---|
| Measurement (`probe`, every frame, zero tokens) | **time**: cuts, freezes, silence, motion curve |
| On-screen text (full-resolution crop, read by the model) | **literal text content** |
| Audio (`audio_probe` + `asr`) | **what was said** — but only after passing the acoustic gate |

Measured payoff: in one real delivery ASR misheard the on-screen caption `大肥鱼` as `大飞鱼` and mangled
a whole line — **3 errors out of 6 lines**, all corrected by the rule "when text conflicts, the picture wins".

---

## Quick start

```powershell
# 1) environment (Windows: full ffmpeg build + ImageMagick 7 + user config)
powershell -ExecutionPolicy Bypass -File scripts\setup-windows.ps1 -VwPy vendor\vw.py
python vendor\vw.py doctor

# 2) build a clip whose ground truth you know
python scripts\build_testclip.py out\

# 3) measurement + visuals
python vendor\vw.py probe out\clip.mp4 --out out\work
python vendor\vw.py grid  --media out\clip.mp4 --frames 25 --cols 5 --out out\work

# 4) acoustics + transcription
python scripts\audio_probe.py out\clip.mp4
python scripts\asr_hallucination_check.py out\clip.mp4 --model small --lang zh
```

Optional audio dependencies:

```bash
pip install faster-whisper "av<19" numpy
```

> **`av<19` is mandatory.** faster-whisper 1.2.1 calls `av.open(..., metadata_errors=...)`,
> a parameter PyAV 19 removed — installing the newest `av` fails with `TypeError` at transcribe time.
> Behind restrictive networks also set `HF_ENDPOINT=https://hf-mirror.com` and
> `HF_HUB_DISABLE_XET=1` (huggingface_hub defaults to the Xet transport, whose host may be unreachable → 401).

---

## Why it runs on Windows

Upstream has two defects that make `grid` / `seq` / `sheet` / `read` **fail outright** on Windows.
Both were located with measured comparisons and patched here (see [`references/windows-setup.md`](references/windows-setup.md)):

| Defect | Measured | Fix |
|---|---|---|
| ffmpeg `drawtext` rejects drive-letter paths | `fontfile=C:/x.ttf` ❌ · `fontfile=C\:/x.ttf` ❌ ← **upstream form** · `fontfile='C\:/x.ttf'` ✅ | wrap the escaped path in single quotes |
| ImageMagick 7 `magick convert` entry | in this build `convert` is the **only** unrecognised subcommand (treated as a filename → `no decode delegate for 'convert'`); `montage`/`identify`/… all fine | degrade `convert` to the bare entry point under 7.x |

Patch: [`patches/vw-windows-fixes.patch`](patches/vw-windows-fixes.patch).

> ⚠️ **Both are environment-dependent — not "every Windows machine hits them".**
> The ffmpeg version/build, whether ImageMagick is the DLL or portable build, and the system locale
> all affect whether they trigger. For instance `drawtext` path parsing differs between builds with
> and without libfreetype/libfontconfig, and `magick convert`'s compatibility entry varies across
> 6.x and different 7.x patch levels.
>
> So read this as a **compatibility note, not a universal bug report**:
> [`references/windows-setup.md`](references/windows-setup.md) gives **measured comparisons plus self-check
> commands** — run the self-check on your own machine, see which behaviour you get, then decide
> whether to apply the patch. It touches two functions and only changes behaviour when
> `os.name == "nt"`, so it is safe on macOS/Linux.

---

## Use it together with browser-skill: watch the scene + do the forensics

This skill turns video into accountable conclusions, but one class of information is structurally
out of its reach: **context** — danmaku, page tags, comments, related videos, anything behind a login.

Paired with [`@wxg-prc-cpg/browser-skill-dsh-plugin`](https://www.npmjs.com/package/@wxg-prc-cpg/browser-skill-dsh-plugin)
(built on Tencent's open-source [BrowserSkill](https://github.com/Tencent/BrowserSkill)),
the result is markedly more complete. A measured example:

- **Offline path (this skill)** gave: frame-accurate measurement, the full burned-in subtitle text,
  six timestamped transcript segments — and **corrected 3 homophone errors** in the ASR output.
- **Live path (browser-skill)** added three things the offline path cannot reach:
  page tags (`搞笑 / AI / claude / deepseek娘` — which pinned down what the on-screen
  blue→orange transformation was actually referring to), a danmaku line that delivered the punchline
  (`坏了，检测到国区用户了` — picture and audio only showed the character vanishing,
  **never why**), and related videos confirming a whole meme series.

> **The offline path answers "what happened"; the live path answers "in what context".**

Division of labour, a recommended 8-step workflow, and browser-skill's prerequisites plus four
measured pitfalls: [`references/with-browser-skill.md`](references/with-browser-skill.md).

---

## Strongly recommended: let an agent do the install

Both this skill and browser-skill only count as "installed" once they actually run, and the install
path is full of details **you only learn by hitting them**: whose PATH is the relevant PATH, whether
the daemon can detach from the current process tree, whether the port matches, which mirror the model
comes from, what version range `av` must be in…

So instead of working through the docs step by step, hand the repo to an agent:

> "Install the environment per `SKILL.md`, verify with `vendor/vw.py doctor` and
> `scripts/build_testclip.py`, and record every pitfall you hit into `references/`"

Why: an agent **verifies by reading source instead of guessing** (both defects here were located by
reading `escape_filter_path()` and running controlled comparisons), **keeps digging past failures**
(faced with "TCP fails but HTTP succeeds", a human tends to stop at "network problem"), and
**turns the process into a reproducible script** (`scripts/setup-windows.ps1` came from exactly that).

Humans only need to do the parts that **must** be done by a human — such as installing and
authorising the browser extension.

---

## Repository layout

```
SKILL.md                        the skill itself (operating manual for the agent)
README.md / README.en.md        this file
LICENSE                         MIT (this project's parts)
NOTICE                          upstream attribution and derivation
patches/vw-windows-fixes.patch  two Windows fixes to upstream vw.py
scripts/
  audio_probe.py                acoustic reconnaissance (new)
  asr_hallucination_check.py    ASR hallucination falsification (new)
  build_testclip.py             ground-truth test clip generator (new)
  setup-windows.ps1             one-shot Windows environment setup (new)
references/
  windows-setup.md              every Windows dependency trap, with measurements
  verification.md               the three-way reconciliation playbook
verification/record-*.md        real run records with ground-truth comparison
vendor/                         upstream MIT code (patched vw.py + vwtools.py + upstream LICENSE)
```

---

## Known limits — please read before use

- **Acoustic reconnaissance does not identify content.** It answers "does this sound like speech",
  not "what was said"; it cannot name a song or a speaker.
- **ASR makes homophone errors.** When a picture exists, the picture wins.
- **Never trust text inside a contact sheet** — always crop and enlarge.
- **Motion is read from frame sequences ordered by frame number**; a 4-frame gap resolves only
  0.1–0.5 s actions (temporal aliasing).
- "Cover the whole clip" and "read every character" cannot both be had.
- This skill only **understands** video. It does **not** edit.

---

## License

MIT. `vendor/` contains code from upstream
[`CFITCorporation/video-watch-skill`](https://github.com/CFITCorporation/video-watch-skill)
(MIT, Copyright © 2026 CFITSec). See [NOTICE](NOTICE) for attribution and derivation details.
