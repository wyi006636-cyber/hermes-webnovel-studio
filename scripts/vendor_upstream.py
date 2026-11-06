#!/usr/bin/env python3
"""Rebuild ``webnovel_studio/_vendor/webnovel-writer`` from the pinned upstream commit.

The vendored engine is a *reduced* copy of ``lingfengQAQ/webnovel-writer``:
only the files the Hermes runtime executes are kept, and a small, reviewed set
of patches (``vendor-patches/*.patch``) is applied on top. Running this script
from a clean checkout must reproduce the committed vendor tree byte for byte,
so reviewers can audit every difference from upstream.

Usage::

    git clone https://github.com/lingfengQAQ/webnovel-writer.git /tmp/upstream
    python scripts/vendor_upstream.py --upstream /tmp/upstream
    python scripts/vendor_upstream.py --upstream /tmp/upstream --check

The script only reads the local upstream clone (``git archive``); it performs
no network access itself.
"""

from __future__ import annotations

import argparse
import io
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

UPSTREAM_REPOSITORY = "https://github.com/lingfengQAQ/webnovel-writer"
UPSTREAM_TAG = "v6.2.1"
UPSTREAM_COMMIT = "59654ccaa17f240c5ae41fe51db9443284f8ca1f"
UPSTREAM_SUBDIR = "webnovel-writer"

REPO_ROOT = Path(__file__).resolve().parents[1]
VENDOR_DIR = REPO_ROOT / "webnovel_studio" / "_vendor" / "webnovel-writer"
PATCH_DIR = REPO_ROOT / "vendor-patches"

# Top-level entries of the upstream plugin directory that the runtime uses.
KEEP_TOP_LEVEL = {"LICENSE", "README.md", "scripts", "templates", "references", "dashboard"}

# Paths (relative to the upstream plugin directory) removed from the kept
# entries: tests, Claude Code packaging/release tooling, and frontend sources
# whose build output (dashboard/frontend/dist) is shipped instead.
DROP_PREFIXES = (
    "scripts/tests/",
    "scripts/data_modules/tests/",
    "dashboard/frontend/src/",
)
DROP_FILES = {
    "scripts/conftest.py",
    "scripts/run_tests.ps1",
    "scripts/requirements.txt",
    "scripts/run_behavior_evals.py",
    "scripts/sync_plugin_version.py",
    "scripts/validate_plugin_package.py",
    "scripts/validate_release_notes.py",
    "dashboard/requirements.txt",
    "dashboard/frontend/design.md",
    "dashboard/frontend/index.html",
    "dashboard/frontend/package.json",
    "dashboard/frontend/package-lock.json",
    "dashboard/frontend/vite.config.js",
}


def _keep(relative: str) -> bool:
    path = PurePosixPath(relative)
    if not path.parts or path.parts[0] not in KEEP_TOP_LEVEL:
        return False
    if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
        return False
    if relative in DROP_FILES:
        return False
    return not any(relative.startswith(prefix) for prefix in DROP_PREFIXES)


def _run(*args: str, cwd: Path) -> bytes:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True).stdout


def build(upstream: Path, destination: Path) -> None:
    commit = _run("git", "rev-parse", f"{UPSTREAM_COMMIT}^{{commit}}", cwd=upstream).decode().strip()
    if commit != UPSTREAM_COMMIT:
        raise SystemExit(f"upstream clone does not contain {UPSTREAM_COMMIT}")
    archive = _run("git", "archive", "--format=tar", UPSTREAM_COMMIT, UPSTREAM_SUBDIR, cwd=upstream)
    prefix = f"{UPSTREAM_SUBDIR}/"
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar.getmembers():
            if not member.isfile() or not member.name.startswith(prefix):
                continue
            relative = member.name[len(prefix):]
            if not _keep(relative):
                continue
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = tar.extractfile(member)
            assert extracted is not None
            target.write_bytes(extracted.read())
    for patch in sorted(PATCH_DIR.glob("*.patch")):
        # Patches use paths relative to the vendor root (a/scripts/..., b/scripts/...).
        subprocess.run(["git", "apply", "-p1", str(patch)], cwd=destination, check=True)


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--upstream", type=Path, required=True, help="local clone of the upstream repository")
    parser.add_argument("--check", action="store_true", help="verify the committed vendor tree instead of rewriting it")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as scratch:
        staging = Path(scratch) / "stage" / "webnovel-writer"
        staging.mkdir(parents=True)
        build(args.upstream.resolve(), staging)
        if args.check:
            expected, actual = _tree(staging), _tree(VENDOR_DIR)
            if expected != actual:
                missing = sorted(set(expected) - set(actual))
                extra = sorted(set(actual) - set(expected))
                changed = sorted(k for k in set(expected) & set(actual) if expected[k] != actual[k])
                print(f"vendor tree drift: missing={missing[:10]} extra={extra[:10]} changed={changed[:10]}")
                return 1
            print(f"vendor tree matches {UPSTREAM_COMMIT} + {len(list(PATCH_DIR.glob('*.patch')))} patch(es)")
            return 0
        if VENDOR_DIR.exists():
            shutil.rmtree(VENDOR_DIR)
        VENDOR_DIR.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(staging, VENDOR_DIR)
    print(f"wrote {VENDOR_DIR.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
