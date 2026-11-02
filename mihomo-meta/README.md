# Automatic stable source selection

The package Makefile automatically selects and downloads the latest official
stable Mihomo release before OpenWrt scans the package version. All update logic
lives in this feed; firmware repositories only need to use the feed.

The checked-in version and source hash pin 1.19.33 as the fallback. If the latest
release query or source download fails, the normal OpenWrt downloader uses the
pinned fallback and verifies its fixed SHA-256. A fallback download or checksum
failure stops the build during source download. Package metadata remains
available offline. Alpha, draft and prerelease tags are excluded.

The result is locked in `tmp/mihomo-meta-selected.mk` for the build tree, including
the real package version and SHA-256. Recursive and parallel makes reuse that
selection; a file lock prevents concurrent downloads from choosing different
versions. The source uses OpenWrt's deterministic Git archive format and is
verified by the normal OpenWrt downloader as well.

A fresh CI build selects the latest stable version automatically. To refresh a
reused local build tree, run this from the OpenWrt root before rebuilding:

```sh
python3 feeds/nikki/mihomo-meta/latest.py \
  --output tmp/mihomo-meta-selected.mk --download-dir dl
./scripts/feeds update -i nikki
make defconfig
```

Run `python3 tests/test_latest.py` from the feed root for offline selection and
fallback tests.
