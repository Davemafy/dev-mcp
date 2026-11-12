"""Minimal offline tests for glTF inspector and chunked artifact retrieval."""
import base64
import json
from pathlib import Path
import struct
import subprocess
import sys

import pytest

import vm_worker


def test_glb_inspection(tmp_path):
    obj = {"asset": {"version": "2.0"}, "meshes": [{"primitives": [
        {"attributes": {"POSITION": 0}, "mode": 4}]}],
        "accessors": [{"count": 9}], "materials": [{"name": "test"}]}
    raw = json.dumps(obj).encode("utf-8")
    raw += b" " * ((4 - len(raw) % 4) % 4)
    blob = struct.pack("<4sII", b"glTF", 2, 20 + len(raw))
    blob += struct.pack("<I4s", len(raw), b"JSON") + raw
    file = tmp_path / "sample.glb"
    file.write_bytes(blob)
    script = Path(__file__).resolve().parents[1] / "tools" / "scene_audit.py"
    result = subprocess.run([sys.executable, str(script), "inspect", str(file)],
                            capture_output=True, text=True, check=True)
    value = json.loads(result.stdout)
    assert value["triangle_count"] == 3
    assert value["mesh_count"] == 1
    assert value["materials"] == 1


def test_artifact_chunk_bounded_scoped(tmp_path, monkeypatch):
    monkeypatch.setattr(vm_worker, "ROOT", tmp_path)
    folder = tmp_path / ("a" * 32)
    folder.mkdir()
    content = b"\x00\x01\x02" * 10000
    (folder / "scene.glb").write_bytes(content)
    first = vm_worker.execute("artifact_read", {
        "worker_id": "a" * 32, "path": "scene.glb", "chunk_size": 12000})
    second = vm_worker.execute("artifact_read", {
        "worker_id": "a" * 32, "path": "scene.glb", "offset": first["next_offset"],
        "chunk_size": 12000})
    assert base64.b64decode(first["data_base64"]) == content[:12000]
    assert base64.b64decode(second["data_base64"]) == content[12000:24000]
    assert not first["complete"] and second["next_offset"] == 24000
    with pytest.raises(ValueError):
        vm_worker.execute("artifact_read", {
            "worker_id": "a" * 32, "path": "../outside.png"})
    with pytest.raises(ValueError):
        vm_worker.execute("artifact_read", {
            "worker_id": "a" * 32, "path": "scene.glb", "chunk_size": 40000})
    (folder / "secret.env").write_text("SECRET")
    with pytest.raises(ValueError):
        vm_worker.execute("artifact_read", {
            "worker_id": "a" * 32, "path": "secret.env"})


def test_prober_has_no_optional_dependency_requirement():
    script = Path(__file__).resolve().parents[1] / "tools" / "scene_audit.py"
    result = subprocess.run([sys.executable, str(script), "probe"],
                            capture_output=True, text=True, check=True)
    assert "cgroup" in json.loads(result.stdout)
