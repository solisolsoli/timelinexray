"""Licensing, notices and documented limits (Milestone 6, P7 section 8)."""

from __future__ import annotations

import json
import tomllib
import unittest

from tests.support import REPO_ROOT

DISCLAIMER = (
    "Independent community analysis of publicly available source code. "
    "Not affiliated with or endorsed by X or xAI."
)
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".bmp", ".tif",
                  ".tiff", ".pdf", ".ai", ".eps"}


def _tracked_like_files() -> list:
    skip = {".git", "__pycache__", "build", "dist"}
    return [path for path in sorted(REPO_ROOT.rglob("*"))
            if path.is_file() and not skip.intersection(path.relative_to(REPO_ROOT).parts)
            and not any(part.endswith(".egg-info") for part in path.parts)]


class NoticeTest(unittest.TestCase):
    def test_notice_states_license_disclaimer_and_runtime_fetching(self) -> None:
        notice = (REPO_ROOT / "NOTICE").read_text("utf-8")
        for text in ("Apache License, Version 2.0", "SPDX-License-Identifier: Apache-2.0",
                     DISCLAIMER, "fetched at runtime", "it is not vendored",
                     "does not include or redistribute source code",
                     "uses no X or xAI logos", "no third-party runtime dependencies"):
            with self.subTest(text=text):
                self.assertIn(text, notice)

    def test_package_metadata_carries_the_license_files(self) -> None:
        project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text("utf-8"))["project"]
        self.assertEqual(project["license"], "Apache-2.0")
        self.assertEqual(project["license-files"], ["LICENSE", "NOTICE"])
        self.assertEqual(project["dependencies"], [])
        self.assertIn("Apache License 2.0", (REPO_ROOT / "README.md").read_text("utf-8"))

    def test_no_logos_or_other_image_assets(self) -> None:
        images = [str(path.relative_to(REPO_ROOT)) for path in _tracked_like_files()
                  if path.suffix.lower() in IMAGE_SUFFIXES]
        self.assertEqual(images, [])

    def test_goldens_hold_citations_not_source(self) -> None:
        data = json.loads((REPO_ROOT / "goldens" / "citations.json").read_text("utf-8"))
        for golden in data["goldens"]:
            self.assertLessEqual(len(golden["anchor"]), 80)
            self.assertLessEqual(set(golden), {"id", "source", "commit", "path", "start_line",
                                               "end_line", "anchor", "span_sha256", "expected",
                                               "history"})


class LimitsTest(unittest.TestCase):
    def test_every_known_limit_category_is_documented(self) -> None:
        limits = (REPO_ROOT / "docs" / "limits.md").read_text("utf-8")
        for text in ("Public defaults are not production values", "No model weights",
                     "Lexical parser only", "Tested clients", "untested",
                     "`EXTERNAL_RECHECK`", "A hash chain is not a signature",
                     "runs on GitHub-hosted runners", "Windows is not supported", "stdio only",
                     "not legal advice", "setuptools>=77"):
            with self.subTest(text=text):
                self.assertIn(text, limits)
        for name in ("README.md", "SECURITY.md"):
            with self.subTest(document=name):
                self.assertIn("docs/limits.md", (REPO_ROOT / name).read_text("utf-8"))


if __name__ == "__main__":
    unittest.main()
