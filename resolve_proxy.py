"""
resolve_proxy.py — Headless DaVinci Resolve Studio proxy generator

Standalone, stdlib-only module. Given an input media path
(including RAW formats Resolve natively decodes — BRAW, RED R3D, Sony X-OCN,
ARRI, etc.), produces a proxy next to it (or at a given output path).
Defaults to H.264 .mp4; pass codec="prores422hq" (etc.) for a ProRes .mov.

Requires: DaVinci Resolve Studio installed locally, with scripting enabled.

Usage as a library (single file):
    from resolve_proxy import transcode
    transcode("/path/to/clip.braw")

Usage as a library (queue of files — launches/quits Resolve only once):
    from resolve_proxy import transcode_batch
    transcode_batch([
        "/path/to/clip1.r3d",
        "/path/to/clip2.mxf",
        "/path/to/clip3.mxf",
    ])

Usage from the command line (one or more files):
    python3 resolve_proxy.py /path/to/clip.braw
    python3 resolve_proxy.py /path/to/clip.braw -o /path/to/output.mp4
    python3 resolve_proxy.py /path/to/clip1.r3d /path/to/clip2.mxf --output-dir /proxies
    python3 resolve_proxy.py --help
"""

import sys
import os
import re
import time
import subprocess

# ---------------------------------------------------------------------------
# Resolve — paths and startup
# ---------------------------------------------------------------------------
RESOLVE_SCRIPT_PATH = (
    "/Library/Application Support/Blackmagic Design"
    "/DaVinci Resolve/Developer/Scripting/Modules"
)
RESOLVE_BIN             = "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/MacOS/Resolve"
RESOLVE_STARTUP_TIMEOUT = 120  # seconds

# Ceiling on a single clip's render. Prevents a wedged Resolve from spinning
# the render-progress poll forever in an unattended batch. Generous on purpose
# (real RAW transcodes can be long) so it only fires on a genuine hang;
# reaching it fails just that clip and the batch moves on.
RESOLVE_RENDER_TIMEOUT = 3600  # seconds (1 h per clip)

# ---------------------------------------------------------------------------
# Default proxy settings — override per-call via function args, or per-run
# via CLI flags (--width, --height, --codec, --suffix, --output-dir). Change
# these if you want new defaults everywhere.
# ---------------------------------------------------------------------------
DEFAULT_WIDTH   = 1920
DEFAULT_HEIGHT  = 1080
DEFAULT_CODEC   = "h264"

# Codec choices -> Resolve's SetCurrentRenderFormatAndCodec(format, codec)
# arguments, plus the container extension used to build the default output
# suffix ("_proxy" + extension) when no explicit suffix/output path is given.
CODEC_PRESETS = {
    "h264":           {"format": "mp4", "codec": "H264",           "extension": ".mp4"},
    "prores422proxy": {"format": "mov", "codec": "ProRes422P",     "extension": ".mov"},
    "prores422lt":    {"format": "mov", "codec": "ProRes422LT",    "extension": ".mov"},
    "prores422":      {"format": "mov", "codec": "ProRes422",      "extension": ".mov"},
    "prores422hq":    {"format": "mov", "codec": "ProRes422HQ",    "extension": ".mov"},
    "prores4444":     {"format": "mov", "codec": "ProRes4444",     "extension": ".mov"},
    "prores4444xq":   {"format": "mov", "codec": "ProRes4444XQ",   "extension": ".mov"},
}

if RESOLVE_SCRIPT_PATH not in sys.path:
    sys.path.insert(0, RESOLVE_SCRIPT_PATH)

try:
    import DaVinciResolveScript as dvr
except ImportError:
    # Don't exit at import time, so the module stays importable (and its pure
    # helpers usable) without Resolve installed. ensure_resolve_running()
    # raises RuntimeError lazily instead.
    dvr = None


# ---------------------------------------------------------------------------
# R3D chunk filtering
# ---------------------------------------------------------------------------
# RED cameras split long takes into numbered chunks (..._001.R3D, _002.R3D,
# ...). The first chunk's metadata already references the rest and plays
# through them as one continuous clip, so feeding Resolve later chunks as
# independent files is redundant at best (and can error on a fragment).
_R3D_CHUNK_RE = re.compile(r"^(?P<clip>.+)_(?P<chunk>\d{3})\.r3d$", re.IGNORECASE)


def filter_r3d_chunks(paths):
    """Keep only the first chunk (_001.R3D) of each R3D clip; drop _002, _003...
    Non-R3D paths (or R3D names that don't follow the _NNN convention) pass
    through unchanged. Preserves input order. Call it on your file list
    before transcode_batch() if you're pointing this at raw camera folders."""
    kept = []
    for path in paths:
        m = _R3D_CHUNK_RE.match(os.path.basename(path))
        if not m or m.group("chunk") == "001":
            kept.append(path)
        else:
            print(f"  skipping {os.path.basename(path)} — not the first chunk "
                  f"of clip '{m.group('clip')}' (already covered by _001.R3D)")
    return kept


def _running_resolve_pids():
    result = subprocess.run(["pgrep", "-x", "Resolve"], capture_output=True, text=True)
    return result.stdout.split() if result.returncode == 0 else []


def _is_headless(pid):
    cmd = subprocess.run(["ps", "-p", pid, "-o", "command="],
                         capture_output=True, text=True).stdout
    return "-nogui" in cmd


def _wait_for_scripting_api(timeout=RESOLVE_STARTUP_TIMEOUT):
    """Poll dvr.scriptapp("Resolve") until it answers or `timeout` elapses.
    Returns the resolve instance, or None on timeout."""
    print(f"  Waiting for scripting API (up to {timeout}s)...", end="", flush=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        resolve = dvr.scriptapp("Resolve")
        if resolve is not None:
            print(" ready.")
            return resolve
        time.sleep(3)
        print(".", end="", flush=True)
    print()
    return None


def ensure_resolve_running():
    """
    Connect to Resolve, launching it headless (-nogui) if it isn't already
    running, then wait for the scripting API to come up.

    NOTE: -rr (remote rendering) does NOT work for scripting-driven renders —
    StartRendering queues the job into a "waiting for remote controller" queue
    and no file is ever written. -nogui is the mode that actually renders.

    Refuses (RuntimeError) if Resolve is already running with its GUI: a
    scripted render's CreateProject/SetCurrentTimeline calls would visibly
    switch whatever project is open there. Close Resolve first.

    If an already-running headless instance never answers the scripting API,
    it's treated as a wedged orphan from an earlier crashed run: it is killed
    and one fresh launch is attempted.

    Returns (resolve_instance, we_launched_it) — the second value tells the
    caller whether it's responsible for quitting Resolve afterward.

    Raises RuntimeError if DaVinciResolveScript can't be imported, a GUI
    session is open, or the API never comes up.
    """
    if dvr is None:
        raise RuntimeError(
            f"Could not import DaVinciResolveScript (expected at "
            f"{RESOLVE_SCRIPT_PATH}) — make sure DaVinci Resolve Studio is "
            f"installed with scripting enabled."
        )

    pids = _running_resolve_pids()
    if any(not _is_headless(pid) for pid in pids):
        raise RuntimeError(
            "DaVinci Resolve is open with its GUI — close it so a headless "
            "instance can be started for scripted rendering."
        )

    already_running = bool(pids)
    if not already_running:
        print("  Resolve not running — launching headless (-nogui)...")
        subprocess.Popen([RESOLVE_BIN, "-nogui"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        print("  Resolve already running headless.")

    resolve = _wait_for_scripting_api()
    if resolve is not None:
        return resolve, not already_running

    if already_running:
        print("  Scripting API never responded on the existing instance — "
              "treating it as a wedged orphan, killing and retrying once...")
        for pid in _running_resolve_pids():
            if _is_headless(pid):
                subprocess.run(["kill", "-9", pid])
        time.sleep(2)
        subprocess.Popen([RESOLVE_BIN, "-nogui"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        resolve = _wait_for_scripting_api()
        if resolve is not None:
            return resolve, True

    raise RuntimeError(
        f"Resolve did not become ready within {RESOLVE_STARTUP_TIMEOUT}s.")


def _render_clip(resolve, input_path, output_path, width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT,
                  codec=DEFAULT_CODEC):
    """
    Render a single clip within an already-connected Resolve session.
    Creates and tears down its own scratch project so clips don't interfere
    with each other. Returns the output path on success, None on failure.
    """
    pm = resolve.GetProjectManager()
    output_dir  = os.path.dirname(output_path)
    output_name = os.path.splitext(os.path.basename(output_path))[0]

    # --- Create a fresh project ---
    # On a cold Resolve launch, scriptapp("Resolve") can return before the
    # Project Manager backend is actually ready — CreateProject silently
    # returns None for the first few seconds. Retry briefly before giving up.
    project_name = f"PROXY_{os.path.basename(input_path)}"
    existing = pm.LoadProject(project_name)
    if existing:
        pm.CloseProject(existing)
        pm.DeleteProject(project_name)

    project = None
    deadline = time.time() + 30
    while time.time() < deadline:
        project = pm.CreateProject(project_name)
        if project is not None:
            break
        time.sleep(2)

    if project is None:
        print(f"ERROR: Could not create Resolve project '{project_name}'.")
        return None

    # --- Import clip ---
    media_pool    = project.GetMediaPool()
    media_storage = resolve.GetMediaStorage()
    clips = media_storage.AddItemListToMediaPool([input_path])
    if not clips:
        print(f"ERROR: Resolve could not import: {input_path}")
        pm.CloseProject(project)
        pm.DeleteProject(project_name)
        return None

    # --- Build timeline ---
    timeline = media_pool.CreateEmptyTimeline("Transcode")
    media_pool.AppendToTimeline(clips)
    project.SetCurrentTimeline(timeline)
    duration = timeline.GetEndFrame() - timeline.GetStartFrame()
    print(f"  Timeline: {duration} frames")

    # --- Render settings: load a matching preset base, enforce format/codec ---
    render_format = CODEC_PRESETS[codec]["format"]
    render_codec  = CODEC_PRESETS[codec]["codec"]
    if render_format == "mp4":
        presets = project.GetRenderPresets()
        for name in (list(presets.values()) if presets else []):
            if "H.264" in name or "H264" in name or "MP4" in name or "YouTube" in name:
                if project.LoadRenderPreset(name):
                    break
    # Must come AFTER loading a preset — presets override the format/codec otherwise
    project.SetCurrentRenderFormatAndCodec(render_format, render_codec)
    project.SetRenderSettings({
        "TargetDir":    output_dir,
        "CustomName":   output_name,
        "FormatWidth":  width,
        "FormatHeight": height,
        "ExportVideo":  True,
        "ExportAudio":  True,
    })

    # --- Render ---
    job_id = project.AddRenderJob()
    if not job_id:
        print("ERROR: Could not queue render job.")
        pm.CloseProject(project)
        pm.DeleteProject(project_name)
        return None

    project.StartRendering([job_id])
    print("  Rendering... ", end="", flush=True)
    render_deadline = time.time() + RESOLVE_RENDER_TIMEOUT
    while project.IsRenderingInProgress():
        if time.time() > render_deadline:
            print()
            print(f"ERROR: render still in progress after "
                  f"{RESOLVE_RENDER_TIMEOUT}s — giving up on this clip.")
            try:
                project.StopRendering()
            except Exception as e:
                print(f"  (StopRendering failed: {e.__class__.__name__}: {e})")
            pm.CloseProject(project)
            pm.DeleteProject(project_name)
            return None
        time.sleep(2)
        print(".", end="", flush=True)
    print(" done.")

    # --- Cleanup this clip's scratch project ---
    # Must close it first — DeleteProject silently fails on the currently-open project.
    pm.CloseProject(project)
    pm.DeleteProject(project_name)

    # --- Confirm output ---
    if os.path.exists(output_path):
        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        print(f"  Saved: {output_path}  ({size_mb:.1f} MB)")
        return output_path
    else:
        print(f"WARNING: Output file not found at expected path: {output_path}")
        return None


def transcode(input_path, output_path=None, output_dir=None, width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT,
              codec=DEFAULT_CODEC, suffix=None):
    """
    Transcode a single input_path to a proxy via headless DaVinci Resolve.
    Connects, renders, and quits Resolve (if this call launched it) — a
    convenience wrapper around transcode_batch() for one file.

    output_dir: optional directory to place the output in (ignored if
                output_path is given). Created automatically if it doesn't
                already exist.

    Returns the output path on success, or None on failure.
    """
    results = transcode_batch([input_path], output_paths=[output_path] if output_path else None,
                               output_dir=output_dir, width=width, height=height, codec=codec, suffix=suffix)
    return results[input_path]


def transcode_batch(input_paths, output_paths=None, output_dir=None,
                     width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT, codec=DEFAULT_CODEC, suffix=None):
    """
    Transcode a queue of clips to proxies, launching Resolve only once for
    the whole batch (much faster than one launch per file).

    input_paths:  list of source media paths (mixed formats OK — BRAW, R3D, MXF, etc.)
    output_paths: optional list of output paths, same length/order as input_paths.
                  If omitted, defaults to '<input><suffix>' next to each source
                  (or inside output_dir, if given).
    output_dir:   optional directory to place all outputs in (ignored per-file
                  if output_paths is given). Defaults to next to each source file.
                  Created automatically if it doesn't already exist.
    width/height: proxy resolution (defaults to 1920x1080)
    codec:        one of CODEC_PRESETS' keys — "h264" (default), "prores422proxy",
                  "prores422lt", "prores422", "prores422hq", "prores4444", "prores4444xq"
    suffix:       appended to the input's basename when no output path is given
                  (defaults to "_proxy" + the chosen codec's container extension,
                  e.g. "_proxy.mp4" for h264 or "_proxy.mov" for any ProRes flavor)

    Returns a dict mapping input_path -> output_path (or None on failure).
    """
    if codec not in CODEC_PRESETS:
        raise ValueError(f"Unknown codec '{codec}'. Choices: {', '.join(CODEC_PRESETS)}")
    if suffix is None:
        suffix = "_proxy" + CODEC_PRESETS[codec]["extension"]

    if output_paths is None:
        output_paths = [None] * len(input_paths)
    if len(output_paths) != len(input_paths):
        raise ValueError("output_paths must be the same length as input_paths")

    resolved_outputs = []
    for input_path, output_path in zip(input_paths, output_paths):
        if output_path is None:
            base, _ = os.path.splitext(input_path)
            if output_dir:
                output_path = os.path.join(output_dir, os.path.basename(base) + suffix)
            else:
                output_path = base + suffix
        resolved_outputs.append(output_path)

    # Create each output's destination directory if it doesn't exist yet
    # (covers --output-dir, an explicit -o/--output, and default output_paths).
    for output_path in resolved_outputs:
        dest_dir = os.path.dirname(output_path)
        if dest_dir:
            os.makedirs(dest_dir, exist_ok=True)

    print("")
    print("DaVinci Resolve")
    resolve, we_launched = ensure_resolve_running()
    print(f"  Connected: {resolve.GetVersionString()}")

    results = {}
    total = len(input_paths)
    try:
        for i, (input_path, output_path) in enumerate(zip(input_paths, resolved_outputs), start=1):
            print("")
            print(f"[{i}/{total}] {os.path.basename(input_path)}")
            print(f"  Output: {output_path}")
            results[input_path] = _render_clip(resolve, input_path, output_path, width, height, codec)
    finally:
        if we_launched:
            resolve.Quit()

    print("")
    print("Batch summary")
    for input_path in input_paths:
        status = "OK" if results[input_path] else "FAILED"
        print(f"  [{status}] {input_path}")

    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Headless DaVinci Resolve proxy generator (single file or batch)."
    )
    parser.add_argument("inputs", nargs="+", help="one or more source media files")
    parser.add_argument("-o", "--output", help="explicit output path (single-file only)")
    parser.add_argument("--output-dir", help="directory to place all outputs in "
                                              "(created automatically if it doesn't exist)")
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH, help=f"proxy width (default {DEFAULT_WIDTH})")
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT, help=f"proxy height (default {DEFAULT_HEIGHT})")
    parser.add_argument("--codec", choices=sorted(CODEC_PRESETS), default=DEFAULT_CODEC,
                         help=f"render codec (default '{DEFAULT_CODEC}')")
    parser.add_argument("--suffix", default=None,
                         help="filename suffix when no explicit output is given "
                              "(default '_proxy' + the codec's container extension, "
                              "e.g. '_proxy.mp4' for h264 or '_proxy.mov' for ProRes)")
    args = parser.parse_args()

    if args.output and len(args.inputs) > 1:
        parser.error("--output can only be used with a single input file")

    print("")
    print("=" * 45)
    print("  Resolve Proxy Generator")
    print("=" * 45)

    try:
        if len(args.inputs) == 1:
            print(f"  Input: {args.inputs[0]}")
            transcode(args.inputs[0], args.output, output_dir=args.output_dir, width=args.width,
                      height=args.height, codec=args.codec, suffix=args.suffix)
        else:
            print(f"  Queue: {len(args.inputs)} file(s)")
            transcode_batch(args.inputs, output_dir=args.output_dir,
                             width=args.width, height=args.height, codec=args.codec, suffix=args.suffix)
    except RuntimeError as e:
        print(f"ERROR: {e}")
        sys.exit(1)
