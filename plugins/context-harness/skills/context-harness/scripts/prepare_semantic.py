#!/usr/bin/env python3
"""Explicitly download pinned public model files. Does not read project sources."""
import argparse
import hashlib
import json
import urllib.request
import urllib.error
from pathlib import Path

REPOSITORY = "Xenova/multilingual-e5-small"
REVISION = "761b726dd34fb83930e26aab4e9ac3899aa1fa78"
FILES = {
    "onnx/model_quantized.onnx": (118308185, "f80102d3f2a1229f387d3c81909990d8945513e347b0eab049f7de3c6f98c193"),
    "tokenizer.json": (17082730, "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39"),
}


def valid(path, size, digest):
    return path.is_file() and path.stat().st_size == size and hashlib.sha256(path.read_bytes()).hexdigest() == digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    target = args.output.expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    manifest = {"repository": REPOSITORY, "revision": REVISION, "files": {}}
    for name, (size, expected) in FILES.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://huggingface.co/{REPOSITORY}/resolve/{REVISION}/{name}"
        partial = path.with_suffix(path.suffix + ".part")
        if not valid(path, size, expected):
            for attempt in range(3):
                if valid(partial, size, expected):
                    break
                offset = partial.stat().st_size if partial.exists() else 0
                if offset >= size:
                    partial.replace(partial.with_suffix('.invalid'))
                    offset = 0
                print(f"Downloading {name}, attempt {attempt + 1}, offset {offset}", flush=True)
                request = urllib.request.Request(url, headers={"Range": f"bytes={offset}-"} if offset else {})
                try:
                    with urllib.request.urlopen(request, timeout=60) as response:
                        append = offset and response.status == 206
                        if append and not response.headers.get('Content-Range', '').startswith(f'bytes {offset}-'):
                            raise ValueError("Unexpected partial response")
                        with partial.open("ab" if append else "wb") as output:
                            while block := response.read(1024 * 1024):
                                output.write(block)
                except (urllib.error.URLError, TimeoutError):
                    print(f"Network interruption for {name}; partial download retained", flush=True)
            if not valid(partial, size, expected):
                raise ValueError(f"Download incomplete or checksum mismatch: {name}; rerun to resume")
            partial.replace(path)
        manifest["files"][name] = {"sha256": expected, "bytes": size}
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"model_dir": str(target), **manifest}, indent=2))


if __name__ == "__main__":
    main()
