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

> **ffmpeg looks at every frame (zero tokens) + acoustic measurement judges whether the track even
> sounds like speech. The model only looks at a few dozen frames and assigns names.**

One-line criterion: **measurement nominates candidates, eyes assign names; acoustics judge plausibility, ASR supplies content, and the ASR VAD has the final say on whether speech exists.**

---

## Three things it does that "sample frames and show the model" does not

### 1. Acoustic reconnaissance — measure whether speech is plausible before trusting any transcript

`scripts/audio_probe.py` uses numpy only, derives **recomputable** physical quantities from the waveform
and spectrum, and returns a verdict. Every number below is a measured value from
[`verification/record-2026-09-30.md`](verification/record-2026-09-30.md):

| Signal | Pure BGM | Speech | Pure tone | What it actually discriminates |
|---|---|---|---|---|
| Onset-envelope autocorrelation peak (beat) | **0.857** | 0.165 | 0.001 | steady beat ↔ none |
| Per-second loudness swing | **6.3 dB** | **19.1 dB** | 98.9 dB | continuous music bed ↔ speech with starts and stops |
| Tonal concentration | low | low | **0.576** | isolates "pure tone / alert beep" |
| 85% spectral rolloff | **466 Hz** | **1277 Hz** | 445 Hz | presence of high-frequency transients |
| Spectral flatness | 0.0001 | 0.023 | 0.00003 | harmonic structure (**only** separates "noise ↔ not noise") |

Note the last row: **spectral flatness alone cannot separate music from a pure tone** (both are very low);
what isolates a pure tone is **tonal concentration**. The verdict reads all of these together — no single
one decides on its own.

It performs **no recognition** — only measurement, the same design philosophy as `probe`.
It therefore often returns an honest abstention:

```
verdict  no clear speech signature, and no music criterion met (confidence low)
  - music criterion not met
  - tonal concentration 0.202, spectral flatness 0.0226
  - hand this track to ASR and trust the VAD result
```

### 2. Hallucination falsification — was the transcript heard, or fabricated?

`scripts/asr_hallucination_check.py` runs both "VAD on" and "all rejection thresholds removed,
force output", then decides with two criteria — **both share one precondition: the VAD produced no
output at all**; either one is enough:

- **Criterion 1** — VAD produces no output, **and** forced mode emits **identical** text in disjoint
  time windows;
- **Criterion 2** — VAD produces no output **and** the model self-reports `no_speech` ≥ 0.4.
  VAD does not depend on the language model, so its silence and the model's own `no_speech` are two
  independent pieces of evidence; when both say "nobody is talking", fluent text does not override them.

> That precondition matters. On the 78-second clip mentioned below, the VAD found 18 real lines while
> forced mode degenerated into a stream of `'这'` / `'这这'` / `'这这这'` — repeated text in disjoint
> windows. Without the precondition, criterion 1 would brand a plainly speech-bearing track as a
> hallucination; with it, the tool correctly returns "transcript trustworthy, but segmentation differs —
> recommend review".

Real case (a clip that is 40% silence plus a pure 440 Hz sine — **no speech whatsoever**):

```
== control (VAD on) ==
  → 0 segments

== experiment (VAD off, thresholds removed, forced output) ==
  [    0.00 →     2.00] no_speech=0.573 logprob=-0.99 | '字幕by索兰娅'
  → 1 segment

== verdict ==
  hallucination: this track very likely has no speech
   - VAD produced no output at all (that detector does not depend on the language model)
   - forced mode produced just 1 segment, mean self-reported no_speech 0.57
   - forced-mode content: ['字幕by索兰娅'] (a single fluent segment proves nothing)
```

Criterion 1 catches a different shape: **the same template line repeated across the whole track.**
On a real Bilibili video whose audio is pure BGM, three disjoint windows emitted identical text:

```
P1  [ 0.00 →  2.00]  no_speech=0.440  '字幕by索兰娅'
P1  [30.00 → 48.00]  no_speech=0.735  '字幕by索兰娅'
P2  [ 0.00 → 15.56]  no_speech=0.508  '字幕by索兰娅'
```

Self-reported `no_speech` is 0.44–0.74 — **the model itself did not think anyone was talking, and yet it
wrote the same line three times.**

On a track that really does contain speech, the same tool reports **trustworthy** and explains why
(all 6 segments differ, `no_speech` mean 0.04).

### 3. Three-way reconciliation — measurement / on-screen text / audio track

| Channel | Authoritative for |
|---|---|
| Measurement (`probe`, every frame, zero tokens) | **time**: cuts, freezes, silence, motion curve |
| On-screen text (full-resolution crop, read by the model) | **literal text content** |
| Audio (`audio_probe` + `asr`) | **what was said** — but only after passing the acoustic gate |

Measured payoff: in one real delivery ASR misheard the on-screen caption `大肥鱼` as `大飞鱼`, rendered
`claude` as `Cloud` throughout, and mangled a whole line — **only 2 of the 6 segments were error-free**,
and all of it was corrected by the rule "when text conflicts, the picture wins".

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

Division of labour, a recommended 8-step workflow, and browser-skill's prerequisites plus five
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
  with-browser-skill.md         pairing with browser-skill: division of labour + measured traps (new)
verification/record-*.md        real run records with ground-truth comparison
vendor/                         upstream MIT code (patched vw.py + vwtools.py + upstream LICENSE)
```

---

## Known limits — please read before use

- **Acoustic reconnaissance does not identify content.** It answers "does this sound like speech",
  not "what was said"; it cannot name a song or a speaker.
- **ASR makes homophone errors.** When a picture exists, the picture wins.
- **Never trust text inside a contact sheet** — always crop and enlarge.
- **Motion only reads from consecutive frames** (the `seq` subcommand — `--times "3.2,3.7,4.2"` grabs
  adjacent frames). It resolves roughly **0.1–0.5 s** of action at a time: sample too far apart and fast
  motion is simply skipped, with no intermediate frames to look at.
- "Cover the whole clip" and "read every character" cannot both be had.
- This skill only **understands** video. It does **not** edit.
- **The acoustic layer has no "this is speech" verdict.** It emits only four: silence / pure tone /
  music (including "probably music") / criterion-not-met. Whether speech is actually present is
  ultimately said by the ASR VAD. On the 20 s clip used above, the acoustic layer returned
  "criterion not met (confidence low)".
- **Live browsing is a different channel, not a substitute.** It reaches danmaku / page tags / comments,
  but it samples coarsely and **cannot capture audio**; when you need something quotable, still use the
  file-based path.
- **On long material the event frames get eaten by the earliest windows.** The event budget is
  first-come-first-served: after the skeleton takes 18 slots only 6 event slots remain (3 frames each).
  Measured on a synthetic 2-hour timeline: all 6 events landed in the first six 120 s windows — i.e. the
  **first 12 minutes** — leaving the remaining two hours with no event frame at all. Read the
  "maximum blind spot" line `plan` prints as a **lower bound**.

**Not verified yet** (the repository's own acceptance record lists these as "not covered")

- **Long clips**: the repository's own acceptance record only goes up to **48 seconds**.
  Skeleton/event allocation for long videos is *derived from the code* (skeleton gap = duration ÷ 16),
  not measured. A **143-second** clip was additionally run while writing this README and behaved
  normally (skeleton gap 8.95 s, still capped at 4 sheets ≈ 1384 tokens), and the event budget being
  consumed by the earliest windows was reproduced on a synthetic 2-hour timeline — but anything much
  longer than that is still untested.
- **Cross-platform**: every measurement was taken on Windows. The patches only change behaviour when
  `os.name == "nt"`, but macOS/Linux have **never been acceptance-tested**.
- **GPU**: `device="cuda"` fails on this machine (missing `cublas64_12.dll`); all ASR measurements ran on **CPU int8**.
- **OCR channel**: `vw.py ocr` was never tested (upstream itself calls it an *index of where text is*,
  not a way to read text).

---

## License

MIT. `vendor/` contains code from upstream
[`CFITCorporation/video-watch-skill`](https://github.com/CFITCorporation/video-watch-skill)
(MIT, Copyright © 2026 CFITSec). See [NOTICE](NOTICE) for attribution and derivation details.
