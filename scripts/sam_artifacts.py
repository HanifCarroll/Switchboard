#!/usr/bin/env python3
"""Finish Next.js assets and remove development files from SAM's Python builds."""

import argparse
import json
import shutil
import zipfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / ".aws-sam" / "build"
LOCAL = ROOT / "aws" / "local"


def website(artifacts):

    # 1. Copy the standalone server and its current public and static files.
    frontend = ROOT / "frontend"
    shutil.copytree(
        frontend / ".next" / "standalone",
        artifacts,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(".env*"),
    )
    shutil.copytree(frontend / "public", artifacts / "public", dirs_exist_ok=True)
    shutil.copytree(
        frontend / ".next" / "static",
        artifacts / ".next" / "static",
        dirs_exist_ok=True,
    )
    current = [
        str(path.relative_to(artifacts))
        for path in (artifacts / ".next" / "static").rglob("*")
        if path.is_file()
    ]
    (artifacts / "release-static.json").write_text(json.dumps(current))

    # 2. Keep the previous release's assets for browsers with older HTML.
    previous = LOCAL / "previous-website.zip"
    if previous.exists():
        with zipfile.ZipFile(previous) as archive:
            retained = set(json.loads(archive.read("release-static.json")))
            for name in retained:
                if (
                    name.startswith(".next/static/")
                    and ".." not in Path(name).parts
                    and not name.endswith("/")
                ):
                    target = artifacts / name
                    if not target.exists():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(archive.read(name))

    # 3. Start Next on the same port as Lambda Web Adapter.
    launcher = artifacts / "run.sh"
    launcher.write_text(
        '#!/bin/sh\nexport HOSTNAME=0.0.0.0\nexport PORT="${PORT:-8081}"\n'
        "exec node /var/task/server.js\n"
    )
    launcher.chmod(0o755)


def clean():
    template = yaml.safe_load((BUILD / "template.yaml").read_text())
    receipts = {}
    for name in ["API", "Worker", "Receiver", "Website"]:
        directory = (
            BUILD / template["Resources"][name]["Properties"]["CodeUri"]
        ).resolve()
        if not directory.is_relative_to(BUILD.resolve()):
            raise ValueError(f"{name} has not been built by SAM")

        if name != "Website":
            for relative in [
                "tests",
                "evals",
                "data/local",
                ".venv",
                ".pytest_cache",
                ".ruff_cache",
            ]:
                shutil.rmtree(directory / relative, ignore_errors=True)
        for path in list(directory.rglob(".env*")):
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        for path in list(directory.rglob("__pycache__")):
            shutil.rmtree(path)

        size = sum(
            path.stat().st_size for path in directory.rglob("*") if path.is_file()
        )
        layer_size = 6 * 1024**2 if name == "Website" else 59807429
        if size + layer_size >= 250 * 1024**2:
            raise ValueError(f"{name} exceeds Lambda's uncompressed package limit")
        receipts[name] = {"unpacked_bytes_with_layers": size + layer_size}
    LOCAL.mkdir(parents=True, exist_ok=True)
    (LOCAL / "packaging.json").write_text(json.dumps(receipts, indent=2) + "\n")
    print("SAM artifacts sanitized; package sizes including layers checked.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["website", "clean"])
    parser.add_argument("artifacts", nargs="?", type=Path)
    arguments = parser.parse_args()
    if arguments.action == "website":
        if arguments.artifacts is None:
            parser.error("website requires the SAM artifacts directory")
        website(arguments.artifacts)
    else:
        clean()


if __name__ == "__main__":
    main()
