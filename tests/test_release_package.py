import hashlib
import json
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts.package_release import build, collect_files


def source(tmp_path):
    root = tmp_path / "project"
    for name, data in {"pyproject.toml": '[project]\nname="silicon-market"\nversion="0.0.1"\n',
                       "README.md": "# Silicon Market\n", "LICENSE": "MIT\n", "app/main.py": "# source\n",
                       "app/static/index.html": "<main>local app</main>", "docs/assets/v0.0.1/chart.svg": "<svg/>"}.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)
    return root


def test_publication_allowlist_excludes_live_data_and_credentials(tmp_path):
    root = source(tmp_path)
    forbidden = ["data/market.sqlite3", "data/browser_profile/Cookies", ".env.local", "output/screen.png",
                 "app/__pycache__/main.pyc", "app/.env.production", "docs/auth.json", "scripts/local.key",
                 "dist/old.zip", ".release/checkout/README.md", "app/data/private.txt"]
    for name in forbidden:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test-only sentinel, must never be published")
    result = build(root, root / "dist")
    with zipfile.ZipFile(result["artifacts"][0]) as archive:
        entries = archive.namelist()
        assert all("/"+path not in "\n".join(entries) for path in forbidden)
        assert "silicon-market-v0.0.1/data/.gitkeep" in entries
        manifest = json.loads(archive.read("silicon-market-v0.0.1/RELEASE_MANIFEST.json"))
        assert not manifest["includes_user_data"] and not manifest["includes_credentials"]
        for row in manifest["files"]:
            assert hashlib.sha256(archive.read("silicon-market-v0.0.1/"+row["path"])).hexdigest() == row["sha256"]


def test_build_is_repeatable_and_tar_matches_zip_sources(tmp_path):
    root = source(tmp_path)
    first = build(root, tmp_path / "a")
    second = build(root, tmp_path / "b")
    assert [Path(path).read_bytes() for path in first["artifacts"]] == [Path(path).read_bytes() for path in second["artifacts"]]
    with zipfile.ZipFile(first["artifacts"][0]) as archive, tarfile.open(first["artifacts"][1], "r:gz") as tar:
        assert set(archive.namelist()) == {entry.name for entry in tar.getmembers()}
        assert all(archive.read(entry.name) == tar.extractfile(entry).read() for entry in tar.getmembers())


def test_symlink_cannot_smuggle_outside_files_into_source(tmp_path):
    root = source(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("not a publication source")
    (root / "app/escape.py").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        collect_files(root)


def test_missing_required_source_is_not_a_release(tmp_path):
    root = source(tmp_path)
    (root / "LICENSE").unlink()
    with pytest.raises(ValueError, match="Required source"):
        build(root, tmp_path / "dist")


def test_explicit_source_file_rejects_symlink_parent(tmp_path):
    root = source(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "ci.yml").write_text("not a publication source")
    (root / ".github").mkdir()
    (root / ".github/workflows").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        collect_files(root)
