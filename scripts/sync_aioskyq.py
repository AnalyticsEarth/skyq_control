"""Copy aioskyq/src/aioskyq into the integration so HACS installs work without PyPI.

    python3 scripts/sync_aioskyq.py          # update the bundled copy
    python3 scripts/sync_aioskyq.py --check  # CI: fail if the copy is stale

aioskyq/src/aioskyq is the source of truth; never edit the bundled copy directly.
"""

from __future__ import annotations

import filecmp
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "aioskyq" / "src" / "aioskyq"
TARGET = ROOT / "custom_components" / "skyq_control" / "aioskyq"
HEADER = "# Bundled copy of aioskyq; edit aioskyq/src/aioskyq and run scripts/sync_aioskyq.py.\n"


def expected_files() -> dict[str, bytes]:
    files = {}
    for path in sorted(SOURCE.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            content = path.read_bytes()
            if path.suffix == ".py":
                content = HEADER.encode() + content
            files[str(path.relative_to(SOURCE))] = content
    return files


def main() -> int:
    expected = expected_files()
    actual = (
        {
            str(p.relative_to(TARGET)): p.read_bytes()
            for p in TARGET.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts
        }
        if TARGET.exists()
        else {}
    )
    if "--check" in sys.argv:
        stale = sorted(set(expected) ^ set(actual) | {n for n in expected if actual.get(n) != expected[n]})
        if stale:
            print("Bundled aioskyq is out of date:", ", ".join(stale), "\nRun: python3 scripts/sync_aioskyq.py")
            return 1
        print("Bundled aioskyq is up to date.")
        return 0
    if TARGET.exists():
        shutil.rmtree(TARGET)
    for name, content in expected.items():
        dest = TARGET / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
    print(f"Copied {len(expected)} files to {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    filecmp.clear_cache()
    sys.exit(main())
