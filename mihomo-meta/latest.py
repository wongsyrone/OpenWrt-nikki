#!/usr/bin/env python3
"""Select and download the latest stable Mihomo source, retaining the pinned fallback."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.error
import urllib.request

REPOSITORY = "https://github.com/MetaCubeX/mihomo.git"
LATEST_RELEASE = "https://api.github.com/repos/MetaCubeX/mihomo/releases/latest"


def latest_tag():
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "OpenWrt-nikki"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    with urllib.request.urlopen(urllib.request.Request(LATEST_RELEASE, headers=headers),
                                timeout=30) as response:
        release = json.load(response)
    tag = release.get("tag_name", "")
    if release.get("draft") or release.get("prerelease") or not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise ValueError("Latest release is not a stable version tag")
    return tag


def source_archive(tag, directory, expected_hash=None):
    version = tag.removeprefix("v")
    name = f"mihomo-meta-{version}"
    destination = directory / f"{name}.tar.gz"
    if expected_hash and destination.is_file():
        if hashlib.sha256(destination.read_bytes()).hexdigest() == expected_hash:
            return destination, expected_hash
    with tempfile.TemporaryDirectory(dir=directory) as scratch:
        scratch = Path(scratch)
        repository = scratch / "repository"
        environment = dict(os.environ, LC_ALL="C")

        def run(*args, **kwargs):
            return subprocess.run(args, check=True, capture_output=True, timeout=300,
                                  env=environment, **kwargs)

        run("git", "init", "-q", str(repository))
        run("git", "-C", str(repository), "fetch", "--depth=1", "--no-tags",
            REPOSITORY, f"refs/tags/{tag}")
        timestamp = run("git", "-C", str(repository), "show", "-s", "--format=%ct",
                        "FETCH_HEAD", text=True).stdout.strip()
        run("git", "-C", str(repository), "archive", "--format=tar",
            f"--output={scratch / 'source.tar'}", "FETCH_HEAD")
        (scratch / name).mkdir(mode=0o755)
        run("tar", "-xf", str(scratch / "source.tar"), "-C", str(scratch / name))
        archive = scratch / "source.tar.gz"
        with archive.open("wb") as output:
            tar = subprocess.Popen(
                ["tar", "--numeric-owner", "--owner=0", "--group=0", "--mode=a-s",
                 "--sort=name", f"--mtime=@{timestamp}", "-c", name],
                cwd=scratch, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            try:
                subprocess.run(["gzip", "-n", "-c"], stdin=tar.stdout,
                               stdout=output, check=True, timeout=300)
                tar.stdout.close()
                _, error = tar.communicate(timeout=30)
                if tar.returncode:
                    raise RuntimeError(error.decode(errors="replace"))
            finally:
                if tar.poll() is None:
                    tar.kill()
                    tar.wait()
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if expected_hash and digest != expected_hash:
            raise ValueError(f"Source checksum mismatch for {tag}")
        archive.replace(destination)
    return destination, digest


def select(makefile, directory):
    fallback = dict(re.findall(r"^(PKG_\w+):=(.*)$", makefile, re.MULTILINE))
    tag = fallback["PKG_SOURCE_VERSION"]
    directory.mkdir(parents=True, exist_ok=True)
    try:
        candidate = latest_tag()
        if tuple(map(int, candidate[1:].split("."))) < tuple(map(int, tag[1:].split("."))):
            raise ValueError("Latest release is older than the pinned fallback")
        archive, digest = source_archive(
            candidate, directory,
            fallback["PKG_MIRROR_HASH"] if candidate == tag else None,
        )
        tag = candidate
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Latest stable Mihomo unavailable ({type(error).__name__}); using {tag}.")
        # Keep package metadata available offline; OpenWrt downloads the pinned fallback.
        archive = directory / f"mihomo-meta-{tag[1:]}.tar.gz"
        digest = fallback["PKG_MIRROR_HASH"]
    for key, value in {
        "PKG_VERSION": tag[1:],
        "PKG_SOURCE_VERSION": tag,
        "PKG_BUILD_VERSION": tag,
        "PKG_MIRROR_HASH": digest,
    }.items():
        makefile, count = re.subn(rf"^{key}:=.*$", f"{key}:={value}", makefile, flags=re.MULTILINE)
        if count != 1:
            raise ValueError(f"Expected one {key} in the fallback Makefile")
    return makefile, archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--download-dir", type=Path, required=True)
    parser.add_argument("--cached", action="store_true", help="Reuse an existing build selection")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.with_suffix(".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.cached and args.output.is_file():
            return
        makefile, archive = select(Path(__file__).with_name("Makefile").read_text(), args.download_dir)
        fields = re.findall(
            r"^(?:PKG_VERSION|PKG_SOURCE_VERSION|PKG_BUILD_VERSION|PKG_MIRROR_HASH):=.*$",
            makefile, re.MULTILINE,
        )
        if len(fields) != 4:
            raise ValueError("Invalid selected version metadata")
        temporary = args.output.with_suffix(".tmp")
        temporary.write_text("\n".join(fields) + "\n")
        temporary.replace(args.output)
        print(f"Selected {archive.name}; source and checksum are pinned in {args.output}.")


if __name__ == "__main__":
    main()
