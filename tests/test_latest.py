import importlib.util
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("latest", ROOT / "mihomo-meta/latest.py")
latest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(latest)
MAKEFILE = (ROOT / "mihomo-meta/Makefile").read_text()


class LatestStableTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.directory = Path(self.scratch.name)

    def test_latest_stable_is_selected(self):
        with patch.object(latest, "latest_tag", return_value="v1.19.34"), \
                patch.object(latest, "source_archive", return_value=(self.directory / "latest.tar.gz", "a" * 64)) as download:
            result, _ = latest.select(MAKEFILE, self.directory)
        self.assertIn("PKG_VERSION:=1.19.34\n", result)
        self.assertIn("PKG_SOURCE_VERSION:=v1.19.34\n", result)
        self.assertIn("PKG_MIRROR_HASH:=" + "a" * 64, result)
        download.assert_called_once_with("v1.19.34", self.directory, None)

    def test_failed_latest_download_uses_pinned_fallback(self):
        with patch.object(latest, "latest_tag", return_value="v1.19.34"), \
                patch.object(latest, "source_archive",
                             side_effect=urllib.error.URLError("download failed")) as download:
            result, _ = latest.select(MAKEFILE, self.directory)
        self.assertIn("PKG_VERSION:=1.19.33\n", result)
        self.assertIn("PKG_SOURCE_VERSION:=v1.19.33\n", result)
        self.assertIn("PKG_MIRROR_HASH:=8adee049a9c1f653b9c52fbf20fec74da56288961f857289d540193f38c0fa44\n", result)
        download.assert_called_once_with("v1.19.34", self.directory, None)

    def test_api_failure_and_older_release_use_fallback(self):
        for value in (urllib.error.URLError("offline"), "v1.19.32"):
            with self.subTest(value=value), \
                    patch.object(latest, "latest_tag",
                                 **({"side_effect": value} if isinstance(value, Exception)
                                    else {"return_value": value})), \
                    patch.object(latest, "source_archive",
                                 return_value=(self.directory / "fallback.tar.gz", "fallback")) as download:
                result, _ = latest.select(MAKEFILE, self.directory)
                self.assertIn("PKG_VERSION:=1.19.33\n", result)
                download.assert_not_called()

    def test_offline_selection_preserves_native_fallback_metadata(self):
        with patch.object(latest, "latest_tag", side_effect=ValueError("invalid release")), \
                patch.object(latest, "source_archive", side_effect=OSError("offline")) as download:
            result, archive = latest.select(MAKEFILE, self.directory)
            self.assertIn("PKG_BUILD_VERSION:=v1.19.33\n", result)
            self.assertEqual(archive.name, "mihomo-meta-1.19.33.tar.gz")
            download.assert_not_called()

    def test_prereleases_and_invalid_tags_are_rejected(self):
        for release in ({"tag_name": "v1.19.34", "prerelease": True},
                        {"tag_name": "v1.19.34", "draft": True},
                        {"tag_name": "Alpha"}, {"tag_name": "v1.19.34;echo bad"}):
            with self.subTest(release=release), \
                    patch.object(latest.urllib.request, "urlopen") as response, \
                    patch.object(latest.json, "load", return_value=release):
                response.return_value.__enter__.return_value = object()
                with self.assertRaises(ValueError):
                    latest.latest_tag()

    def test_cached_fallback_requires_matching_checksum(self):
        archive = self.directory / "mihomo-meta-1.19.33.tar.gz"
        archive.write_bytes(b"cached source")
        digest = latest.hashlib.sha256(archive.read_bytes()).hexdigest()
        self.assertEqual(latest.source_archive("v1.19.33", self.directory, digest), (archive, digest))
        with patch.object(latest.subprocess, "run", side_effect=OSError("offline")):
            with self.assertRaises(OSError):
                latest.source_archive("v1.19.33", self.directory, "bad hash")

    def test_cached_selection_does_not_query_releases(self):
        output = self.directory / "selected.mk"
        output.write_text("PKG_VERSION:=1.19.33\n")
        with patch("sys.argv", ["latest.py", "--cached", "--output", str(output),
                                 "--download-dir", str(self.directory)]), \
                patch.object(latest, "select") as select:
            latest.main()
            select.assert_not_called()
        self.assertEqual(output.read_text(), "PKG_VERSION:=1.19.33\n")

    def test_source_archive_is_reproducible(self):
        repository = self.directory / "repository"
        repository.mkdir()
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        (repository / "go.mod").write_text("module example.invalid/test\n")
        subprocess.run(["git", "-C", str(repository), "add", "go.mod"], check=True)
        subprocess.run(["git", "-C", str(repository), "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
                        "commit", "-q", "-m", "fixture"], check=True)
        subprocess.run(["git", "-C", str(repository), "tag", "v1.19.34"], check=True)
        first, second = self.directory / "first", self.directory / "second"
        first.mkdir()
        second.mkdir()
        with patch.object(latest, "REPOSITORY", str(repository)):
            archive, digest = latest.source_archive("v1.19.34", first)
            other, other_digest = latest.source_archive("v1.19.34", second, digest)
        self.assertEqual(digest, other_digest)
        self.assertEqual(archive.read_bytes(), other.read_bytes())


if __name__ == "__main__":
    unittest.main()
