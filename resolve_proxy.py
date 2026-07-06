"""
resolve_proxy.py — Headless DaVinci Resolve Studio proxy generator

Standalone module: no OpenCue, no ShareBrowser. Given an input media path
(including RAW formats Resolve natively decodes — BRAW, RED R3D, Sony X-OCN,
ARRI, etc.), produces an H.264 .mp4 proxy next to it (or at a given output path).

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

# ---------------------------------------------------------------------------
# Default proxy settings — override per-call via function args, or per-run
# via CLI flags (--width, --height, --suffix, --output-dir). Change these if
# you want new defaults everywhere.
# ---------------------------------------------------------------------------
DEFAULT_WIDTH   = 1920
DEFAULT_HEIGHT  = 1080
DEFAULT_SUFFIX  = "_proxy.mp4"   # appended to the input's basename when no output path is given

if RESOLVE_SCRIPT_PATH not in sys.path:
    sys.path.insert(0, RESOLVE_SCRIPT_PATH)

try:
    import DaVinciResolveScript as dvr
except ImportError:
    print("ERROR: Could not import DaVinciResolveScript.")
    print(f"  Expected module at: {RESOLVE_SCRIPT_PATH}")
    print("  Make sure DaVinci Resolve Studio is installed.")
    sys.exit(1)


def ensure_resolve_running():
    """
    Start Resolve headless (-nogui) if it isn't already running, then wait
    for the scripting API to come up.

    NOTE: -rr (remote rendering) does NOT work for scripting-driven renders —
    StartRendering queues the job into a "waiting for remote controller" queue
    and no file is ever written. -nogui is the mode that actually renders.

    Returns (resolve_instance, we_launched_it).
    """
    already_running = subprocess.run(
        ["pgrep", "-x", "Resolve"], capture_output=True
    ).returncode == 0

    if not already_running:
        print("  Resolve not running — launching headless (-nogui)...")
        subprocess.Popen(
            [RESOLVE_BIN, "-nogui"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
    else:
        print("  Resolve already running.")

    print(f"  Waiting for scripting API (up to {RESOLVE_STARTUP_TIMEOUT}s)...", end="", flush=True)
    deadline = time.time() + RESOLVE_STARTUP_TIMEOUT
    while time.time() < deadline:
        resolve = dvr.scriptapp("Resolve")
        if resolve is not None:
            print(" ready.")
            return resolve, not already_running
        time.sleep(3)
        print(".", end="", flush=True)

    print()
    print("ERROR: Resolve did not become ready in time.")
    sys.exit(1)


def _render_clip(resolve, input_path, output_path, width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT):
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
        pm.DeleteProject(project_name)
        return None

    # --- Build timeline ---
    timeline = media_pool.CreateEmptyTimeline("Transcode")
    media_pool.AppendToTimeline(clips)
    project.SetCurrentTimeline(timeline)
    duration = timeline.GetEndFrame() - timeline.GetStartFrame()
    print(f"  Timeline: {duration} frames")

    # --- Render settings: load H.264 preset base, enforce MP4 output ---
    presets = project.GetRenderPresets()
    for name in (list(presets.values()) if presets else []):
        if "H.264" in name or "H264" in name or "MP4" in name or "YouTube" in name:
            if project.LoadRenderPreset(name):
                break
    # Must come AFTER loading a preset — presets override the container to .mov otherwise
    project.SetCurrentRenderFormatAndCodec("mp4", "H264")
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
        pm.DeleteProject(project_name)
        return None

    project.StartRendering([job_id])
    print("  Rendering... ", end="", flush=True)
    while project.IsRenderingInProgress():
        time.sleep(2)
        print(".", end="", flush=True)
    print(" done.")

    # --- Cleanup this clip's scratch project ---
    pm.DeleteProject(project_name)

    # --- Confirm output ---
    if os.path.exists(output_path):
        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        print(f"  Saved: {output_path}  ({size_mb:.1f} MB)")
        return output_path
    else:
        print(f"WARNING: Output file not found at expected path: {output_path}")
        return None


def transcode(input_path, output_path=None, width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT,
              suffix=DEFAULT_SUFFIX):
    """
    Transcode a single input_path to an H.264 .mp4 proxy via headless
    DaVinci Resolve. Connects, renders, and quits Resolve (if this call
    launched it) — a convenience wrapper around transcode_batch() for one file.

    Returns the output path on success, or None on failure.
    """
    results = transcode_batch([input_path], output_paths=[output_path] if output_path else None,
                               width=width, height=height, suffix=suffix)
    return results[input_path]


def transcode_batch(input_paths, output_paths=None, output_dir=None,
                     width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT, suffix=DEFAULT_SUFFIX):
    """
    Transcode a queue of clips to H.264 .mp4 proxies, launching Resolve only
    once for the whole batch (much faster than one launch per file).

    input_paths:  list of source media paths (mixed formats OK — BRAW, R3D, MXF, etc.)
    output_paths: optional list of output paths, same length/order as input_paths.
                  If omitted, defaults to '<input><suffix>' next to each source
                  (or inside output_dir, if given).
    output_dir:   optional directory to place all outputs in (ignored per-file
                  if output_paths is given). Defaults to next to each source file.
    width/height: proxy resolution (defaults to 1920x1080)
    suffix:       appended to the input's basename when no output path is given
                  (defaults to "_proxy.mp4")

    Returns a dict mapping input_path -> output_path (or None on failure).
    """
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

    print("")
    print("DaVinci Resolve")
    resolve, we_launched = ensure_resolve_running()
    print(f"  Connected: {resolve.GetVersionString()}")

    results = {}
    total = len(input_paths)
    for i, (input_path, output_path) in enumerate(zip(input_paths, resolved_outputs), start=1):
        print("")
        print(f"[{i}/{total}] {os.path.basename(input_path)}")
        print(f"  Output: {output_path}")
        results[input_path] = _render_clip(resolve, input_path, output_path, width, height)

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
    parser.add_argument("-o", "--output", help="explicit output .mp4 path (single-file only)")
    parser.add_argument("--output-dir", help="directory to place all outputs in (batch)")
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH, help=f"proxy width (default {DEFAULT_WIDTH})")
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT, help=f"proxy height (default {DEFAULT_HEIGHT})")
    parser.add_argument("--suffix", default=DEFAULT_SUFFIX,
                         help=f"filename suffix when no explicit output is given (default '{DEFAULT_SUFFIX}')")
    args = parser.parse_args()

    if args.output and len(args.inputs) > 1:
        parser.error("--output can only be used with a single input file")

    print("")
    print("=" * 45)
    print("  Resolve Proxy Generator")
    print("=" * 45)

    if len(args.inputs) == 1:
        print(f"  Input: {args.inputs[0]}")
        transcode(args.inputs[0], args.output, width=args.width, height=args.height, suffix=args.suffix)
    else:
        print(f"  Queue: {len(args.inputs)} file(s)")
        transcode_batch(args.inputs, output_dir=args.output_dir,
                         width=args.width, height=args.height, suffix=args.suffix)
