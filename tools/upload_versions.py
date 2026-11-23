#!/usr/bin/env python3
"""Upload local version objects to the private R2 bucket through Wrangler.

Run without ``--apply`` first to inspect the plan. Existing source files are
never changed or deleted. Uploading the same key replaces that R2 object, so
``--apply`` is deliberately required.
"""

from __future__ import annotations

import argparse
import hashlib
import mimetypes
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--versions-dir", type=Path, default=ROOT / "versions")
    parser.add_argument("--bucket", default="dvarc-versions")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    files = [path for path in args.versions_dir.rglob("*") if path.is_file()]
    for path in sorted(files):
        relative = path.relative_to(args.versions_dir).as_posix()
        key = f"versions/{relative}"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        command = [
            "npx", "wrangler", "r2", "object", "put", f"{args.bucket}/{key}",
            "--file", str(path), "--remote", "--content-type", content_type,
            "--header", f"x-amz-meta-sha256:{digest}",
        ]
        print(f"{'UPLOAD' if args.apply else 'PLAN  '} {key} ({path.stat().st_size} bytes, sha256={digest})")
        if args.apply:
            subprocess.run(command, cwd=ROOT, check=True)
    if not args.apply:
        print("Nothing uploaded. Re-run with --apply after reviewing the plan.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
