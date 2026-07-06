# resolve-proxy

Headless DaVinci Resolve Studio proxy generator. No OpenCue, no ShareBrowser —
just a standalone module that turns camera-original media (BRAW, RED R3D,
Sony X-OCN, ARRI MXF, and anything else Resolve can import) into H.264 `.mp4`
proxies, driven entirely through the Resolve scripting API.

## Requirements

- DaVinci Resolve **Studio** installed locally (the free version does not
  expose scripted rendering the same way)
- macOS paths are hardcoded at the top of `resolve_proxy.py`:
  - `RESOLVE_SCRIPT_PATH` — the Resolve scripting modules directory
  - `RESOLVE_BIN` — the Resolve binary invoked with `-nogui`

## Usage

Single file:

```python
from resolve_proxy import transcode
transcode("/path/to/clip.braw")
# -> /path/to/clip_DR.mp4 (720p H.264)
```

Batch (launches Resolve once for the whole queue instead of once per file):

```python
from resolve_proxy import transcode_batch
transcode_batch([
    "/path/to/clip1.r3d",
    "/path/to/clip2.mxf",
    "/path/to/clip3.mxf",
])
```

From the command line:

```bash
python3 resolve_proxy.py /path/to/clip.braw
python3 resolve_proxy.py /path/to/clip.braw /path/to/output.mp4
python3 resolve_proxy.py /path/to/clip1.r3d /path/to/clip2.mxf /path/to/clip3.mxf
```

## Notes

- Resolve is launched with `-nogui`, not `-rr`. `-rr` (remote rendering) does
  not work for scripting-driven renders — `StartRendering` queues into a
  "waiting for remote controller" queue and no file is ever written. `-nogui`
  runs fully unattended and quits automatically once this script started it.
- `CreateProject` can return `None` for the first few seconds after a cold
  Resolve launch, before the Project Manager backend is fully up — the module
  retries for up to 30s before giving up.
- Each clip in a batch gets its own scratch Resolve project, created and
  deleted around the render, so clips don't interfere with each other.
