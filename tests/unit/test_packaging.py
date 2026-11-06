"""Packaging invariants for the catalog build."""

from __future__ import annotations

from pathlib import Path

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPOSITORY_ROOT / "webnovel_studio"


def test_plugin_readme_matches_repository_readme() -> None:
    # The catalog renders the README from the plugin subdir at the pinned SHA.
    assert (PLUGIN_ROOT / "README.md").read_bytes() == (REPOSITORY_ROOT / "README.md").read_bytes()


def test_plugin_ships_license_copy() -> None:
    assert (PLUGIN_ROOT / "LICENSE").read_bytes() == (REPOSITORY_ROOT / "LICENSE").read_bytes()


def test_manifest_dependencies_have_upper_bounds() -> None:
    manifest = yaml.safe_load((PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    for requirement in manifest["python_dependencies"]:
        assert ">=" in requirement and "<" in requirement, requirement


def test_plugin_tree_has_no_bytecode_or_install_markers() -> None:
    tracked = [path for path in PLUGIN_ROOT.rglob("*") if "__pycache__" not in path.parts]
    assert not [path for path in tracked if path.suffix in {".pyc", ".pyo"}]
    assert not (PLUGIN_ROOT / ".webnovel-hermes-install.json").exists()


def test_engine_has_no_dotenv_loader_or_network_client() -> None:
    engine = PLUGIN_ROOT / "_vendor" / "webnovel-writer" / "scripts"
    config = (engine / "data_modules" / "config.py").read_text(encoding="utf-8")
    assert "_load_dotenv_file" not in config
    assert 'os.getenv("EMBED_API_KEY"' not in config
    api_client = (engine / "data_modules" / "api_client.py").read_text(encoding="utf-8")
    assert "aiohttp" not in api_client
