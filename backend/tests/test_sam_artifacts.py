"""Keep credentials out of packages and preserve assets from older browser pages."""

import importlib.util
import json
import zipfile
from pathlib import Path

import pytest
import yaml

spec = importlib.util.spec_from_file_location(
    "sam_artifacts", Path(__file__).parents[2] / "scripts" / "sam_artifacts.py"
)
assert spec is not None and spec.loader is not None
artifacts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(artifacts)


def test_python_packages_remove_credentials_and_keep_runtime_data(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(artifacts, "BUILD", tmp_path / "build")
    monkeypatch.setattr(artifacts, "LOCAL", tmp_path / "receipts")
    directory = tmp_path / "build" / "API-Shared"
    for name in [
        ".env.production",
        "tests/test.py",
        "data/local/cache.json",
        "data/fixtures/tickets.json",
        "switchboard/prompts/policy.md",
    ]:
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("canary")
    website = tmp_path / "build" / "Website"
    website.mkdir()
    template = {
        "Resources": {
            name: {
                "Properties": {
                    "CodeUri": "Website" if name == "Website" else "API-Shared"
                }
            }
            for name in ["API", "Worker", "Receiver", "Website"]
        }
    }
    (tmp_path / "build" / "template.yaml").write_text(yaml.safe_dump(template))
    artifacts.clean()
    assert not (directory / ".env.production").exists()
    assert not (directory / "tests").exists()
    assert not (directory / "data/local").exists()
    assert (directory / "data/fixtures/tickets.json").read_text() == "canary"
    assert (directory / "switchboard/prompts/policy.md").read_text() == "canary"


def test_rejects_unbuilt_source_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, "BUILD", tmp_path)
    (tmp_path / "template.yaml").write_text(
        yaml.safe_dump(
            {"Resources": {"API": {"Properties": {"CodeUri": "../backend"}}}}
        )
    )
    with pytest.raises(ValueError, match="has not been built"):
        artifacts.clean()


def test_website_retains_only_previous_static_assets(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, "ROOT", tmp_path)
    monkeypatch.setattr(artifacts, "LOCAL", tmp_path / "local")
    for name in [
        ".next/standalone/server.js",
        ".next/standalone/.env.production",
        ".next/static/current.js",
        "public/icon.svg",
    ]:
        target = tmp_path / "frontend" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("current")
    (tmp_path / "local").mkdir()
    with zipfile.ZipFile(tmp_path / "local/previous-website.zip", "w") as archive:
        names = [
            ".next/static/old.js",
            ".next/static/current.js",
            ".next/static/../../secret",
            "server.js",
        ]
        archive.writestr("release-static.json", json.dumps(names))
        for name in names:
            archive.writestr(name, "old")
    output = tmp_path / "output"
    artifacts.website(output)
    assert (output / ".next/static/old.js").read_text() == "old"
    assert (output / ".next/static/current.js").read_text() == "current"
    assert (output / "server.js").read_text() == "current"
    assert not (output / ".env.production").exists()
    assert not (output / "secret").exists()
    assert "8081" in (output / "run.sh").read_text()
    assert (output / "run.sh").stat().st_mode & 0o111
