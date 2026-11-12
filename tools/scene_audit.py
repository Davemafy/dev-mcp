#!/usr/bin/env python3
"""DevMCP 3D inspection CLI. No display server, GUI editor, or Blender required.

Usage:
  python /opt/devmcp/scene_audit.py probe
  python /opt/devmcp/scene_audit.py inspect scene.glb
  python /opt/devmcp/scene_audit.py compare reference.png render.png --diff diff.png
  python /opt/devmcp/scene_audit.py capture http://127.0.0.1:3000 --output view.jpg --require-webgl
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import struct
import sys


def _emit(value):
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


def probe(_args):
    def text(path):
        try:
            return Path(path).read_text().strip()
        except OSError:
            return None
    result = {
        "python": sys.version.split()[0],
        "modules": {x: importlib.util.find_spec(x) is not None
                    for x in ("numpy", "PIL", "trimesh", "playwright")},
        "cgroup": {"memory_max": text("/sys/fs/cgroup/memory.max"),
                   "cpu_max": text("/sys/fs/cgroup/cpu.max")},
        "playwright_browsers_path": os.getenv("PLAYWRIGHT_BROWSERS_PATH"),
    }
    _emit(result)


def inspect(args):
    path = Path(args.file)
    if path.suffix.lower() != ".glb":
        raise ValueError("inspect currently accepts .glb files only")
    with path.open("rb") as f:
        header = f.read(12)
        if len(header) != 12:
            raise ValueError("Truncated GLB header")
        magic, version, length = struct.unpack("<4sII", header)
        if magic != b"glTF" or version != 2 or length != path.stat().st_size:
            raise ValueError("Invalid GLB v2 header or length")
        chunk = f.read(8)
        if len(chunk) != 8:
            raise ValueError("Missing JSON chunk")
        count, kind = struct.unpack("<I4s", chunk)
        if kind != b"JSON" or count > 20_000_000:
            raise ValueError("Missing or oversized GLB JSON chunk")
        doc = json.loads(f.read(count))
    accessors = doc.get("accessors", [])
    meshes = doc.get("meshes", [])
    tri = 0
    modes = []
    for mesh in meshes:
        for primitive in mesh.get("primitives", []):
            mode = primitive.get("mode", 4)
            modes.append(mode)
            if mode == 4:
                index = primitive.get("indices")
                if index is None:
                    index = primitive.get("attributes", {}).get("POSITION")
                if index is not None:
                    if not isinstance(index, int) or index < 0 or index >= len(accessors):
                        raise ValueError("Invalid GLB accessor")
                    tri += accessors[index]["count"] // 3
    _emit({"file": str(path), "bytes": length, "mesh_count": len(meshes),
           "triangle_count": tri, "materials": len(doc.get("materials", [])),
           "images": len(doc.get("images", [])),
           "nodes": len(doc.get("nodes", [])),
           "extensions_used": doc.get("extensionsUsed", []),
           "non_triangular_primitive_modes": sorted(set(m for m in modes if m != 4))})


def compare(args):
    from PIL import Image, ImageChops, ImageStat
    with Image.open(args.reference) as ref0, Image.open(args.render) as new0:
        ref, new = ref0.convert("RGB"), new0.convert("RGB")
        if ref.size != new.size:
            raise ValueError("Camera comparison requires matching pixel dimensions")
        difference = ImageChops.difference(ref, new)
        means = ImageStat.Stat(difference).mean
        result = {"reference": args.reference, "render": args.render,
                  "resolution": list(ref.size),
                  "mean_absolute_error_rgb": round(sum(means) / 3, 3),
                  "channel_mae": [round(m, 3) for m in means]}
        if args.diff:
            difference.save(args.diff)
            result["diff_image"] = args.diff
        _emit(result)


def capture(args):
    from playwright.sync_api import sync_playwright
    if not (args.url.startswith("http://") or args.url.startswith("https://")):
        raise ValueError("Capture URL must be HTTP or HTTPS")
    out = Path(args.output)
    if out.suffix.lower() not in (".png", ".jpg", ".jpeg"):
        raise ValueError("Capture output must be PNG or JPEG")
    out.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--enable-unsafe-swiftshader", "--use-gl=angle",
                  "--use-angle=swiftshader", "--disable-gpu-sandbox"],
        )
        try:
            page = browser.new_page(viewport={"width": args.width, "height": args.height})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)[:250]))
            page.goto(args.url, wait_until="domcontentloaded", timeout=25000)
            page.wait_for_timeout(args.settle_ms)
            webgl = page.evaluate("""() => {
              const c = document.createElement('canvas');
              const gl = c.getContext('webgl2');
              return !!gl;
            }""")
            page.screenshot(path=str(out), full_page=False)
            result = {"screenshot": str(out), "width": args.width,
                      "height": args.height, "webgl2": webgl, "page_errors": errors,
                      "url": page.url}
            _emit(result)
            if args.require_webgl and not webgl:
                raise RuntimeError("Browser captured a page but WebGL2 is unavailable")
            if errors:
                raise RuntimeError("Page errors occurred; screenshot is not a clean pass")
        finally:
            browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("probe")
    inp = sub.add_parser("inspect")
    inp.add_argument("file")
    diff = sub.add_parser("compare")
    diff.add_argument("reference")
    diff.add_argument("render")
    diff.add_argument("--diff", help="Optional pixel-difference image path")
    cap = sub.add_parser("capture")
    cap.add_argument("url")
    cap.add_argument("--output", required=True)
    cap.add_argument("--width", type=int, default=390)
    cap.add_argument("--height", type=int, default=844)
    cap.add_argument("--settle-ms", type=int, default=1000)
    cap.add_argument("--require-webgl", action="store_true")
    args = parser.parse_args()
    if args.command == "probe":
        probe(args)
    elif args.command == "inspect":
        inspect(args)
    elif args.command == "compare":
        compare(args)
    else:
        capture(args)


if __name__ == "__main__":
    main()
