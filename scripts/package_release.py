"""Build a source-only release with an explicit publication allowlist.

Does not read user databases, browser sessions, environment files or credentials.
No server, phone, model or network operation is performed by this builder.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import stat
import tarfile
import tomllib
import zipfile
from pathlib import Path

SOURCE_DIRS = ("app", "automation", "docs", "tests", "scripts")
SOURCE_FILES = ("README.md", "LICENSE", "CHANGELOG.md", "API_CONTRACT.md", "pyproject.toml",
                "uv.lock", "requirements.txt", "start.sh", "project.sh", "启动硅价.command",
                ".gitignore", ".github/workflows/ci.yml")
FORBIDDEN_DIRS = {".git", ".venv", "venv", "data", "output", "dist", "build", ".release",
                  ".playwright-cli", "browser_profile", "__pycache__", ".pytest_cache",
                  ".ruff_cache", ".mypy_cache", "node_modules", "htmlcov"}


def excluded(relative):
    return (any(part in FORBIDDEN_DIRS or part.endswith(".egg-info") for part in relative.parts)
            or relative.name.startswith(".env") or relative.name in {".DS_Store", ".coverage", "auth.json"}
            or relative.suffix.lower() in {".pyc", ".pyo", ".log", ".db", ".sqlite", ".sqlite3", ".pem", ".key"})


def collect_files(root):
    root = Path(root).resolve()
    selected = {}

    def add(path):
        relative = path.relative_to(root)
        if excluded(relative):
            return
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != root and root in parent.parents):
            raise ValueError(f"Refusing symlink in publication source: {relative}")
        if path.is_file():
            selected[relative.as_posix()] = path

    for name in SOURCE_FILES:
        path = root / name
        if path.exists() or path.is_symlink():
            add(path)
    for name in SOURCE_DIRS:
        base = root / name
        if base.is_symlink():
            raise ValueError(f"Refusing symlink source directory: {name}")
        if not base.is_dir():
            continue
        for directory, dirs, files in os.walk(base, followlinks=False):
            parent = Path(directory)
            for item in list(dirs):
                path = parent / item
                if excluded(path.relative_to(root)):
                    dirs.remove(item)
                elif path.is_symlink():
                    raise ValueError(f"Refusing symlink source directory: {path.relative_to(root)}")
            for item in files:
                add(parent / item)
    for required in ("pyproject.toml", "README.md", "LICENSE", "app/main.py", "app/static/index.html"):
        if required not in selected:
            raise ValueError(f"Required source missing: {required}")
    return dict(sorted(selected.items()))


def _zip(entries, output, prefix):
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, (content, mode) in sorted(entries.items()):
            info = zipfile.ZipInfo(f"{prefix}/{name}", date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)


def build(root, output_dir):
    root, output_dir = Path(root).resolve(), Path(output_dir).resolve()
    files = collect_files(root)
    version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", version):
        raise ValueError("Invalid release version")
    name = f"silicon-market-v{version}"
    entries = {relative: (path.read_bytes(), 0o755 if path.stat().st_mode & 0o111 else 0o644)
               for relative, path in files.items()}
    entries["data/.gitkeep"] = (b"", 0o644)
    manifest = dict(project="silicon-market", version=version, kind="source-release",
                    includes_user_data=False, includes_credentials=False, includes_native_binaries=False,
                    files=[dict(path=path, bytes=len(value[0]), sha256=hashlib.sha256(value[0]).hexdigest(),
                                mode=oct(value[1])) for path, value in sorted(entries.items())])
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2)+"\n").encode()
    entries["RELEASE_MANIFEST.json"] = (manifest_bytes, 0o644)
    output_dir.mkdir(parents=True, exist_ok=True)
    zip_path, tar_path = output_dir / f"{name}.zip", output_dir / f"{name}.tar.gz"
    _zip(entries, zip_path, name)
    with tar_path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for relative, (content, mode) in sorted(entries.items()):
                    info = tarfile.TarInfo(f"{name}/{relative}")
                    info.size, info.mode, info.mtime = len(content), mode, 0
                    archive.addfile(info, io.BytesIO(content))
    visual = {relative: value for relative, value in entries.items() if relative.startswith("docs/assets/")}
    visual_path = output_dir / f"{name}-visual-assets.zip"
    _zip(visual, visual_path, name)
    (output_dir / "release-manifest.json").write_bytes(manifest_bytes)
    artifacts = [zip_path, tar_path, visual_path, output_dir / "release-manifest.json"]
    (output_dir / "SHA256SUMS").write_text("".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in artifacts), encoding="utf-8")
    return dict(version=version, source_files=len(files), entries=len(entries), artifacts=[str(path) for path in artifacts])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.root, args.output_dir or args.root / "dist"), ensure_ascii=False))


if __name__ == "__main__":
    main()
