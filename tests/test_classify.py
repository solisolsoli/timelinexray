"""Ordered classification rules, language guesses and license detection (no git)."""

from __future__ import annotations

import unittest

from timelinexray.snapshot.classify import (
    ClassifierConfig,
    classify,
    guess_language,
    license_hints,
    license_kind,
)

CONFIG = ClassifierConfig(max_file_bytes=100, binary_probe_bytes=16, marker_probe_bytes=32)


def _c(path: str, head: bytes = b"text\n", *, mode: str = "100644", size: int | None = None,
       obj_type: str = "blob"):
    size = len(head) if size is None else size
    return classify(path, mode, obj_type, size, head, CONFIG)


class RuleOrderTest(unittest.TestCase):
    def test_parsed_candidates_are_the_four_languages(self) -> None:
        for path, language in (
            ("a/b.rs", "rust"), ("a/B.scala", "scala"), ("x.sc", "scala"),
            ("a/b.py", "python"), ("a/b.pyi", "python"), ("A.java", "java"),
        ):
            with self.subTest(path):
                result = _c(path)
                self.assertEqual((result.classification, result.language), ("parsed-candidate", language))

    def test_other_text_files(self) -> None:
        for path, language in (("README.md", "markdown"), ("BUILD", "starlark"),
                               ("x.proto", "protobuf"), ("notes.strato", None)):
            with self.subTest(path):
                result = _c(path)
                self.assertEqual((result.classification, result.language), ("text", language))

    def test_submodule_and_symlink_win_over_everything(self) -> None:
        self.assertEqual(_c("vendor/x.py", mode="160000", obj_type="commit", size=None).reason,
                         "submodule")
        self.assertEqual(_c("vendor/x.py", b"\0" * 500, mode="120000").reason, "symlink")

    def test_vendored_directory_segments(self) -> None:
        self.assertEqual(_c("vendor/x.py").rule, "dir:vendor")
        self.assertEqual(_c("a/third_party/b/c.rs").rule, "dir:third_party")
        self.assertEqual(_c("web/node_modules/p/i.js").reason, "vendored")
        self.assertEqual(_c("src/vendor.py").classification, "parsed-candidate")  # a file, not a dir

    def test_generated_by_name_suffix_and_marker(self) -> None:
        self.assertEqual(_c("p/api_pb2.py").rule, "suffix:_pb2.py")
        self.assertEqual(_c("p/api_pb2_grpc.py").rule, "suffix:_pb2_grpc.py")
        self.assertEqual(_c("x/Cargo.lock").rule, "name:Cargo.lock")
        self.assertEqual(_c("g.rs", b"// @generated\n").rule, "marker:@generated")
        self.assertEqual(_c("g.go", b"//Code generated x DO NOT EDIT.\n").rule,
                         "marker:go-generated")
        self.assertEqual(_c("NSFW_Generated_Image.bot").classification, "text")

    def test_marker_only_counts_inside_the_probe(self) -> None:
        self.assertEqual(_c("g.rs", b" " * 40 + b"@generated").classification, "parsed-candidate")

    def test_oversize_boundary(self) -> None:
        self.assertEqual(_c("a.rs", size=100).classification, "parsed-candidate")
        result = _c("a.rs", size=101)
        self.assertEqual((result.reason, result.rule), ("oversize", "size>100"))

    def test_binary_probe_boundary(self) -> None:
        self.assertEqual(_c("a.dat", b"x" * 15 + b"\0").reason, "binary")
        self.assertEqual(_c("a.dat", b"x" * 16 + b"\0").classification, "text")

    def test_oversize_is_reported_before_binary(self) -> None:
        self.assertEqual(_c("a.bin", b"\0", size=1000).reason, "oversize")

    def test_default_config(self) -> None:
        config = ClassifierConfig()
        self.assertEqual((config.max_file_bytes, config.binary_probe_bytes), (1_048_576, 8000))
        self.assertEqual(config.to_dict()["version"], 1)


class LanguageTest(unittest.TestCase):
    def test_guesses(self) -> None:
        self.assertEqual(guess_language("a/Dockerfile"), "dockerfile")
        self.assertEqual(guess_language("a/b.BAZEL"), "starlark")
        self.assertEqual(guess_language("notes.txt"), "plain-text")
        self.assertIsNone(guess_language("a/b.unknownext"))
        self.assertIsNone(guess_language("LICENSE"))


class LicenseTest(unittest.TestCase):
    def test_license_kind(self) -> None:
        cases = {
            "LICENSE": "license", "a/LICENSE.txt": "license", "LICENSE-MIT": "license",
            "Licence.md": "license", "COPYING": "copying", "COPYING.LESSER": "copying",
            "x/NOTICE": "notice", "THIRD_PARTY_NOTICES.md": "third-party-notices",
            "src/license.rs": None, "licenses.py": None, "LICENSES": None, "README.md": None,
        }
        for path, kind in cases.items():
            with self.subTest(path):
                self.assertEqual(license_kind(path), kind)

    def test_license_hints_are_keyword_based(self) -> None:
        self.assertEqual(license_hints(b"Apache License\nVersion 2.0, January 2004"), ("Apache-2.0",))
        self.assertEqual(
            license_hints(b"Permission is hereby granted, free of charge, to any person"), ("MIT",)
        )
        self.assertEqual(license_hints(b"SPDX-License-Identifier: BSD-3-Clause\n"),
                         ("spdx:BSD-3-Clause",))
        self.assertEqual(license_hints(b"no license words here"), ())


if __name__ == "__main__":
    unittest.main()
