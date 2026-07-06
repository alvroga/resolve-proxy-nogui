# resolve-proxy

Headless DaVinci Resolve Studio proxy generator. No OpenCue, no ShareBrowser,
no third-party dependencies — a standalone module that turns camera-original
media (BRAW, RED R3D, Sony X-OCN, ARRI MXF, and anything else Resolve can
import) into H.264 `.mp4` proxies, driven entirely through the Resolve
scripting API.

Handles a single file or a whole queue of mixed-format clips, and runs
unattended (no GUI window, no user interaction required once started).

---

## Requirements

- **DaVinci Resolve Studio** installed locally. The free version does not
  expose scripted rendering the same way — this will not work without Studio.
- Python 3 (stdlib only — no `pip install` needed). `DaVinciResolveScript`,
  the module this script imports, ships with Resolve itself.
- macOS only, as shipped. Two constants at the top of `resolve_proxy.py` are
  macOS-specific paths:
  ```python
  RESOLVE_SCRIPT_PATH = "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules"
  RESOLVE_BIN          = "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/MacOS/Resolve"
  ```
  To port to Windows/Linux: update both to the platform's install paths, and
  replace the `pgrep -x Resolve` process check in `ensure_resolve_running()`
  with a platform-appropriate equivalent (e.g. `tasklist` on Windows).

## Quick start

```bash
# one file, default settings (1920x1080, saved as <name>_proxy.mp4 next to the source)
python3 resolve_proxy.py "/path/to/clip.braw"

# a batch of mixed formats — Resolve launches once for the whole queue, not once per file
python3 resolve_proxy.py "/path/to/clip1.r3d" "/path/to/clip2.mxf" "/path/to/clip3.braw"
```

The first run of the day will pause for ~30–90s while Resolve launches
headless in the background — that's normal. If Resolve is already running,
it connects immediately and skips the launch step.

---

## Command-line usage

```
usage: resolve_proxy.py [-h] [-o OUTPUT] [--output-dir OUTPUT_DIR]
                        [--width WIDTH] [--height HEIGHT] [--suffix SUFFIX]
                        inputs [inputs ...]
```

| Flag | Meaning | Default |
|---|---|---|
| `inputs` | one or more source media file paths (required) | — |
| `-o`, `--output` | explicit output `.mp4` path — **single input file only** | `<input>_proxy.mp4` next to the source |
| `--output-dir` | directory to place all outputs in — batch only | next to each source file |
| `--width` | proxy width in pixels | `1920` |
| `--height` | proxy height in pixels | `1080` |
| `--suffix` | filename suffix used when no explicit output is given | `_proxy.mp4` |

Examples:

```bash
# custom output path for a single file
python3 resolve_proxy.py clip.braw -o /somewhere/else/proxy.mp4

# custom resolution
python3 resolve_proxy.py clip.braw --width 1280 --height 720

# batch: dump every proxy into one folder with a custom suffix
python3 resolve_proxy.py clip1.r3d clip2.mxf clip3.mxf --output-dir /proxies --suffix "_DR.mp4"
```

---

## Python API

```python
from resolve_proxy import transcode, transcode_batch

# single file — connects to Resolve, renders, quits Resolve if this call started it
transcode("/path/to/clip.braw")
# -> /path/to/clip_proxy.mp4 (1080p H.264)

# override resolution / output path / suffix
transcode("/path/to/clip.braw", output_path="/proxies/clip.mp4")
transcode("/path/to/clip.braw", width=1280, height=720, suffix="_DR.mp4")

# batch — launches Resolve exactly once for the whole list, not once per clip
results = transcode_batch([
    "/path/to/clip1.r3d",
    "/path/to/clip2.mxf",
    "/path/to/clip3.braw",
], output_dir="/proxies", width=1280, height=720)

# results is a dict: {input_path: output_path_or_None}
for input_path, output_path in results.items():
    print(input_path, "->", output_path or "FAILED")
```

### Function reference

- **`transcode(input_path, output_path=None, width=1920, height=1080, suffix="_proxy.mp4")`**
  Single-file convenience wrapper around `transcode_batch`. Returns the output
  path, or `None` on failure.

- **`transcode_batch(input_paths, output_paths=None, output_dir=None, width=1920, height=1080, suffix="_proxy.mp4")`**
  Renders a list of clips in one Resolve session. `output_paths`, if given,
  must be the same length/order as `input_paths`. Returns a dict mapping each
  input path to its output path (or `None` if that clip failed) — one failed
  clip does not stop the rest of the batch.

- **`ensure_resolve_running()`**
  Launches Resolve headless (`-nogui`) if it isn't already running, and blocks
  until the scripting API responds (up to `RESOLVE_STARTUP_TIMEOUT`, 120s by
  default). Returns `(resolve_instance, we_launched_it)` — the second value
  tells the caller whether it's responsible for quitting Resolve afterward.

---

## Changing defaults permanently

Edit the constants near the top of `resolve_proxy.py`:

```python
DEFAULT_WIDTH   = 1920
DEFAULT_HEIGHT  = 1080
DEFAULT_SUFFIX  = "_proxy.mp4"
```

These apply everywhere unless overridden per-call (Python) or per-run (CLI flags).

---

## How it works

1. Checks if Resolve is running (`pgrep -x Resolve`); if not, launches it with
   `-nogui` and polls `DaVinciResolveScript.scriptapp("Resolve")` until the
   scripting API responds.
2. For each clip: creates a disposable Resolve project, imports the clip,
   builds a one-clip timeline, forces the render format to MP4/H.264 at the
   requested resolution, queues and runs the render, then deletes the
   scratch project.
3. If this script launched Resolve itself, it quits Resolve when the whole
   batch is done. If Resolve was already running (e.g. you have it open), it
   leaves it running.

## Known gotchas

- **Use `-nogui`, not `-rr`.** `-rr` (remote rendering) does not work for
  scripting-driven renders — `StartRendering` queues the job into a "waiting
  for remote controller" queue and no file is ever produced. This is an
  architectural Resolve limitation, not a permissions issue. `-nogui` is the
  mode that actually renders unattended.
- **Cold-start race on `CreateProject`.** Right after a fresh Resolve launch,
  `scriptapp("Resolve")` can return a valid handle a few seconds *before* the
  Project Manager backend is actually ready — `CreateProject` silently
  returns `None` during that window. The module retries for up to 30s before
  giving up, which is enough in practice.
- **"New version available" update modal.** On some launches, Resolve can pop
  a software-update notification that blocks headless startup until
  dismissed. This is intermittent and not yet handled by this script — if you
  see a run hang at the "waiting for scripting API" step for the full 120s
  timeout, check whether Resolve threw up a GUI dialog despite `-nogui` and
  dismiss it manually. (Disabling auto-update checks in Resolve preferences
  may avoid this, but wasn't required to get the script working.)
- **One project per clip.** Each clip gets its own scratch project so a
  failure or quirk on one clip can't contaminate another's render settings.
