"""``txray export context-layer``: optional Markdown notes for a Context Layer vault.

Everything here runs offline against the synthetic history fixture of the findings tests.
Context Layer itself is never imported or required; the one live check with a real
``context-layer`` executable is ``tests/test_context_layer_live.py`` (optional).
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from timelinexray import __version__
from timelinexray.export import notes as notes_module
from timelinexray.findings import Actor
from timelinexray.export import writer as writer_module
from tests.findings_support import AUTHOR, REVIEWER, HistoryRepo, spec
from tests.support import REPO_ROOT, run_cli

MANIFEST = ".txray-export.json"
INDEX = "txray-index.md"
README = "txray-README.md"
WIKILINK = re.compile(r"\[\[([^\[\]|#]+)(?:[|#][^\[\]]*)?\]\]")

REPO: HistoryRepo
IDS: dict[str, str] = {}
LEDGER: Path


def setUpModule() -> None:
    global REPO, LEDGER
    REPO = HistoryRepo()
    memory = REPO.memory()
    LEDGER = memory.ledger.directory
    r = REPO
    IDS["current"] = memory.add(spec(
        r, title="Filter apply keeps truthy candidates",
        claim="The synthetic apply function keeps every truthy candidate (quokka marker).",
        evidence_class="CODE", component="src",
        citations=[r.cite("base", "src/filters.py", "6-7", "def apply")],
        limitations=["Only the synthetic fixture was read."]), AUTHOR).finding_id
    IDS["stale"] = memory.add(spec(r), AUTHOR).finding_id  # CLICK_WEIGHT, changes at `literal`
    IDS["hostile"] = memory.add(spec(
        r, title="Notes [[evil-title]] <b>bold</b> #tag | pipe `tick`\nsecond line",
        claim="Line one.\n---\n# Heading from the ledger\n[[evil-claim]] and ```` a fence\n",
        evidence_class="REPO_DOC", component="docs [[evil-component]]",
        citations=[r.cite("base", "docs/notes.md", "1", "Notes")],
        limitations=["see [[evil-limit]] <script>x</script>"]), AUTHOR).finding_id
    IDS["dependent"] = memory.add(spec(
        r, title="Inference on the filter", claim="The filter finding implies an ordering.",
        evidence_class="INFERENCE", citations=[], depends_on=[{"finding": IDS["current"]}]),
        AUTHOR).finding_id
    IDS["retracted"] = memory.add(spec(
        r, title="Withdrawn claim", claim="A claim that was withdrawn."), AUTHOR).finding_id
    memory.retract(IDS["retracted"], AUTHOR, "withdrawn")
    memory.verify()
    memory.review(IDS["stale"], REVIEWER, "SUPPORTED", "Checked the span.\nIt holds at base.")
    memory.reanchor(REPO.commits["literal"])
    IDS["unchecked"] = memory.add(spec(
        r, title="Never checked", claim="The FILTERS list names two filters.",
        evidence_class="CODE", citations=[r.cite("base", "src/filters.py", "1-4", "FILTERS")]),
        AUTHOR).finding_id


def tearDownModule() -> None:
    REPO.cleanup()


def export(out: Path, *extra: str, ledger: Path | None = None) -> tuple[int, bytes, bytes]:
    return run_cli(["export", "context-layer", "--out", str(out), "--ledger",
                    str(ledger or LEDGER), "--store", str(REPO.store_dir), *extra])


def tree(root: Path) -> dict[str, bytes]:
    """Every file below ``root`` (symbolic links recorded by their target)."""
    found: dict[str, bytes] = {}
    if not root.exists():
        return found
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_symlink():
            found[rel] = b"-> " + os.readlink(path).encode()
        elif path.is_file():
            found[rel] = path.read_bytes()
    return found


def note_path(key: str) -> str:
    return f"txray-findings/txray-finding-{IDS[key]}.md"


def split_note(text: str) -> tuple[dict[str, Any], str]:
    """Frontmatter parsed with the YAML subset Context Layer reads, and the body."""
    lines = text.split("\n")
    assert lines[0] == "---", lines[0]
    end = lines.index("---", 1)
    meta = {}
    for line in lines[1:end]:
        key, _, value = line.partition(": ")
        meta[key] = _scalar(value)
    return meta, "\n".join(lines[end + 1:])


def _scalar(value: str) -> Any:
    if value.startswith("["):
        inner = value[1:-1]
        return [] if not inner else [_scalar(item) for item in _split_items(inner)]
    if value.startswith('"'):
        return json.loads(value)
    return {"null": None, "true": True, "false": False}.get(
        value, int(value) if value.isdigit() else value)


def _split_items(text: str) -> list[str]:
    items, current, quoted, escaped = [], "", False, False
    for char in text:
        if quoted:
            current += char
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted, current = True, current + char
        elif char == ",":
            items.append(current.strip())
            current = ""
        else:
            current += char
    items.append(current.strip())
    return items


def outside_fences(body: str) -> str:
    """The body without fenced code blocks (where Markdown syntax is inert)."""
    kept, fence = [], None
    for line in body.split("\n"):
        match = re.match(r"^(`{3,})", line)
        if fence is None and match:
            fence = match.group(1)
            continue
        if fence is not None:
            if line.strip() == fence:
                fence = None
            continue
        kept.append(line)
    return "\n".join(kept)


class ExportTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="txray-clx-")
        self.tmp = Path(self._tmp.name)
        self.out = self.tmp / "vault" / "timelinexray"
        self.out.parent.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def ok(self, *extra: str, out: Path | None = None) -> dict[str, Any]:
        code, stdout, stderr = export(out or self.out, *extra, "--json")
        self.assertEqual(code, 0, stderr.decode())
        return json.loads(stdout)["data"]


class SelectionTest(ExportTestCase):
    def test_default_exports_only_current_active_findings(self) -> None:
        data = self.ok()
        files = tree(self.out)
        notes = sorted(name for name in files if name.startswith("txray-findings/"))
        self.assertEqual(notes, sorted(note_path(k) for k in ("current", "hostile", "dependent")))
        self.assertEqual(set(files), {MANIFEST, INDEX, README, *notes})
        self.assertEqual(data["findings"]["exported"], 3)
        self.assertEqual(data["findings"]["not_current"], 0)
        self.assertEqual(data["findings"]["left_out"], {
            "not_current_NOT_CHECKED": 1, "not_current_STALE": 1, "retracted": 1})
        index = files[INDEX].decode()
        for key in ("stale", "unchecked", "retracted"):
            self.assertNotIn(IDS[key], index)
        self.assertIn("`--include-stale` exports them", index)
        manifest = json.loads(files[MANIFEST])
        self.assertEqual(set(manifest["files"]), {INDEX, README, *notes})
        for name, digest in manifest["files"].items():
            self.assertEqual(hashlib.sha256(files[name]).hexdigest(), digest)

    def test_include_stale_labels_findings_that_are_not_current(self) -> None:
        data = self.ok("--include-stale")
        self.assertEqual(data["findings"]["exported"], 5)
        self.assertEqual(data["findings"]["not_current"], 2)
        self.assertEqual(data["findings"]["left_out"], {"retracted": 1})
        self.assertFalse((self.out / f"txray-findings/txray-finding-{IDS['retracted']}.md").exists())
        literal = REPO.commits["literal"]
        for key, value, commit in (("stale", "STALE", literal), ("unchecked", "NOT_CHECKED", None)):
            with self.subTest(key):
                meta, body = split_note((self.out / note_path(key)).read_text("utf-8"))
                self.assertEqual((meta["current"], meta["freshness"], meta["freshness_commit"]),
                                 (False, value, commit))
                self.assertTrue(meta["title"].startswith(f"NOT CURRENT ({value}"))
                self.assertIn("timelinexray-not-current", meta["tags"])
                self.assertIn(f"# NOT CURRENT ({value}", body)
                self.assertIn("**NOT CURRENT.**", body)
                self.assertIn("Do not present it as current", body)
        index = (self.out / INDEX).read_text("utf-8")
        current_part, stale_part = index.split("## Not current (2)")
        self.assertIn(IDS["current"], current_part)
        self.assertNotIn(IDS["stale"], current_part)
        self.assertIn(f"[[txray-finding-{IDS['stale']}]]: NOT CURRENT (STALE", stale_part)

    def test_commit_option_asks_about_one_commit(self) -> None:
        base = REPO.commits["base"]
        data = self.ok("--commit", base[:10])
        self.assertEqual(data["commit"], base)
        exported = {note["finding_id"]: note for note in data["notes"]}
        self.assertEqual(set(exported), {IDS["current"], IDS["stale"], IDS["hostile"]})
        meta, body = split_note((self.out / note_path("stale")).read_text("utf-8"))
        self.assertEqual((meta["freshness"], meta["freshness_commit"], meta["freshness_check"]),
                         ("CURRENT", base, "integrity"))
        self.assertIn("integrity check at the cited commit", body)
        unknown = self.tmp / "vault" / "other"
        code, _, err = export(unknown, "--commit", "abcdef1234")
        self.assertEqual(code, 1)
        self.assertIn(b"not pinned", err)
        self.assertFalse(unknown.exists())


class ContentTest(ExportTestCase):
    def test_frontmatter_fields(self) -> None:
        self.ok()
        meta, _ = split_note((self.out / note_path("current")).read_text("utf-8"))
        literal, base = REPO.commits["literal"], REPO.commits["base"]
        expected = {
            "title": "Filter apply keeps truthy candidates",
            "finding_id": IDS["current"],
            "evidence_status": "SUPPORTED",
            "status_basis": "proposed",
            "status_by": None,
            "workflow": "draft",
            "freshness": "CURRENT",
            "freshness_commit": literal,
            "freshness_check": "reanchor",
            "current": True,
            "evidence_class": "CODE",
            "scope": "public_code",
            "component": "src",
            "source_label": None,
            "repo": "xai-org/x-algorithm",
            "cited_commits": [base],
            "exported_by": f"TimelineXray {__version__}",
            "tags": ["timelinexray", "timelinexray-finding"],
            "up": "[[txray-index]]",
        }
        self.assertEqual({key: meta[key] for key in expected}, expected)
        self.assertRegex(meta["finding_event"], r"^[0-9a-f]{64}$")
        reviewed, _ = split_note(self.export_stale_note())
        self.assertEqual((reviewed["evidence_status"], reviewed["status_basis"],
                          reviewed["status_by"], reviewed["workflow"]),
                         ("SUPPORTED", "reviewed", REVIEWER.name, "reviewed"))

    def export_stale_note(self) -> str:
        out = self.tmp / "vault" / "with-stale"
        self.ok("--include-stale", out=out)
        return (out / note_path("stale")).read_text("utf-8")

    def test_body_claim_citations_and_limitations(self) -> None:
        self.ok()
        _, body = split_note((self.out / note_path("current")).read_text("utf-8"))
        base, literal = REPO.commits["base"], REPO.commits["literal"]
        span = REPO.store.read_span(base, "src/filters.py", 6, 7).sha256
        self.assertIn("```text\nThe synthetic apply function keeps every truthy candidate "
                      "(quokka marker).\n```", body)
        self.assertIn(f"](https://github.com/xai-org/x-algorithm/blob/{base}/src/filters.py"
                      "#L6-L7)", body)
        self.assertIn(f"span sha256 `{span}`", body)
        self.assertIn(f"https://github.com/xai-org/x-algorithm/blob/{literal}/src/filters.py"
                      "#L6-L7", body)  # the same bytes at the freshness commit
        self.assertIn("anchor `def apply`", body)
        self.assertIn("## Limitations\n\n- Only the synthetic fixture was read.", body)
        self.assertIn("re-export to refresh", body)
        self.assertIn("basis `proposed` (proposed by its author, not reviewed)", body)
        self.assertNotIn("public default", body.lower().split("## limitations")[0])

    def test_public_default_wording(self) -> None:
        text = self.export_stale_note()
        base = REPO.commits["base"]
        self.assertIn(f"**Public default at commit `{base}`.** Numbers in this finding and its "
                      "cited code are public defaults at the cited commit, not production "
                      "values", text)
        self.assertIn("Rationale:\n\n```text\nChecked the span.\nIt holds at base.\n```", text)

    def test_readme_states_the_provenance(self) -> None:
        data = self.ok()
        head = data["ledger"]["head"]
        readme = (self.out / README).read_text("utf-8")
        self.assertIn(f"Exported from TimelineXray {__version__}, ledger head `{head}`; status and "
                      "freshness at export time; re-export to refresh.", readme)
        self.assertIn("Independent community analysis of publicly available source code. Not "
                      "affiliated with or endorsed by X or xAI.", readme)
        self.assertIn("[[txray-index]]", readme)
        meta, _ = split_note(readme)
        self.assertEqual(meta["ledger_head"], head)
        index_meta, _ = split_note((self.out / INDEX).read_text("utf-8"))
        self.assertEqual((index_meta["ledger_head"], index_meta["findings"],
                          index_meta["current"]), (head, 3, 3))
        for name, data_bytes in tree(self.out).items():
            with self.subTest(name):  # no local location is ever written
                self.assertNotIn(str(self.tmp).encode(), data_bytes)
                self.assertNotIn(str(REPO.root).encode(), data_bytes)

    def test_wikilinks_resolve_to_exported_notes(self) -> None:
        self.ok()
        files = {name: data.decode() for name, data in tree(self.out).items()
                 if name.endswith(".md")}
        stems = {Path(name).stem for name in files}
        index = files[INDEX]
        for name in files:
            if name.startswith("txray-findings/"):
                self.assertIn(f"[[{Path(name).stem}]]", index)
        for name, text in files.items():
            meta, body = split_note(text)
            links = WIKILINK.findall(outside_fences(body)) + WIKILINK.findall(meta.get("up") or "")
            with self.subTest(name):
                self.assertTrue(links)
                self.assertLessEqual(set(links), stems)
                if name != INDEX:
                    self.assertEqual(meta["up"], "[[txray-index]]")
        dependent = files[note_path("dependent")]
        self.assertIn(f"- finding [[txray-finding-{IDS['current']}]]", dependent)

    def test_ledger_text_cannot_add_links_headings_or_tags(self) -> None:
        self.ok()
        text = (self.out / note_path("hostile")).read_text("utf-8")
        meta, body = split_note(text)
        visible = outside_fences(body)
        for marker in ("[[evil-title]]", "[[evil-claim]]", "[[evil-component]]",
                       "[[evil-limit]]", "<b>", "<script>", "# Heading from the ledger"):
            with self.subTest(marker):
                self.assertNotIn(marker, visible)
        self.assertIn("\\[\\[evil-title\\]\\]", visible)
        self.assertIn(" \\#tag \\| pipe \\`tick\\`", visible)
        self.assertIn("[[evil-claim]]", body)  # verbatim, but only inside the fenced block
        self.assertEqual(meta["title"],
                         "Notes [[evil-title]] <b>bold</b> #tag | pipe `tick` second line")
        self.assertEqual(visible.count("\n# "), 1)
        self.assertNotIn("\n---\n", visible)  # the ledger's "---" line stays inside the fence
        index = (self.out / INDEX).read_text("utf-8")
        self.assertNotIn("[[evil", index)

    def test_yaml_values_and_names(self) -> None:
        self.assertEqual(notes_module.yaml_value("CURRENT"), "CURRENT")
        self.assertEqual(notes_module.yaml_value("true"), '"true"')
        self.assertEqual(notes_module.yaml_value("a\"b\\c\nd\u2028"), '"a\\"b\\\\c\\u000ad\\u2028"')
        self.assertEqual(json.loads(notes_module.yaml_value("x\x1by")), "x\x1by")
        stems = notes_module.note_stems(["P1b:P1-013", "P1b_P1-013", "F-abc", "f-ABC"])
        self.assertEqual(len({stem.casefold() for stem in stems.values()}), 4)
        self.assertTrue(all(re.fullmatch(r"txray-finding-[A-Za-z0-9._-]+", s)
                            for s in stems.values()))
        self.assertEqual(notes_module.note_stems(["F-0123456789ab"]),
                         {"F-0123456789ab": "txray-finding-F-0123456789ab"})
        self.assertEqual(notes_module.permalink("a" * 40, "dir/a b(1).rs", 3, 3),
                         "https://github.com/xai-org/x-algorithm/blob/" + "a" * 40
                         + "/dir/a%20b%281%29.rs#L3")
        self.assertIsNone(notes_module.permalink("abc1234", "x.rs", 1, 2))


class ImportedFindingsTest(ExportTestCase):
    def test_imported_findings_with_unresolved_citations_and_web_sources(self) -> None:
        memory = REPO.memory()
        memory.import_file(REPO_ROOT / "schemas" / "research-import.example.yaml", "EXAMPLE",
                           Actor("importer-a", "importer"))
        ledger = memory.ledger.directory
        code, stdout, err = export(self.out, "--json", ledger=ledger)
        self.assertEqual(code, 0, err)
        exported = json.loads(stdout)["data"]["findings"]
        # the code-citing findings are UNVERIFIABLE (the commit is not pinned); the two
        # web-only findings have nothing to re-verify and are exported as external evidence
        self.assertEqual((exported["exported"], exported["external"], exported["current"]),
                         (2, 2, 2))
        self.assertEqual(exported["left_out"], {"not_current_UNVERIFIABLE": 4})
        code, stdout, err = export(self.out, "--include-stale", "--json", ledger=ledger)
        self.assertEqual(code, 0, err)
        names = sorted(Path(n["path"]).name for n in json.loads(stdout)["data"]["notes"])
        self.assertIn("txray-finding-EXAMPLE_EX-001.md", names)
        meta, body = split_note(
            (self.out / "txray-findings" / "txray-finding-EXAMPLE_EX-001.md").read_text("utf-8"))
        self.assertEqual((meta["finding_id"], meta["status_basis"], meta["workflow"],
                          meta["source_label"], meta["current"]),
                         ("EXAMPLE:EX-001", "reported", "imported", "EXAMPLE", False))
        self.assertIn("not resolved (commit_not_pinned); no permalink", body)
        self.assertNotIn("https://github.com/", body)
        self.assertIn("**Public default at commit", body)
        web = [name for name in names if name.endswith(("EX-005.md", "EX-006.md"))]
        self.assertEqual(len(web), 2)
        text = (self.out / "txray-findings" / web[0]).read_text("utf-8")
        self.assertIn("## External sources", text)
        self.assertIn("<https://example.com/statements/synthetic>", text)


class DeterminismTest(ExportTestCase):
    def test_reexport_is_byte_identical(self) -> None:
        first = self.ok("--include-stale")
        other = self.tmp / "vault" / "second"
        self.ok("--include-stale", out=other)
        self.assertEqual(tree(self.out), tree(other))
        before = {name: (self.out / name).stat().st_mtime_ns for name in tree(self.out)}
        again = self.ok("--include-stale")
        self.assertEqual(again["files"]["written"], [])
        self.assertEqual(again["files"]["removed"], [])
        self.assertEqual(sorted(again["files"]["unchanged"]),
                         sorted(first["files"]["written"]))
        self.assertEqual({name: (self.out / name).stat().st_mtime_ns for name in before}, before)
        self.assertEqual(tree(self.out), tree(other))

    def test_human_output(self) -> None:
        code, out, err = export(self.out)
        self.assertEqual(code, 0, err)
        text = out.decode()
        self.assertIn("exported   3 finding(s) (3 current, 0 not current)", text)
        self.assertIn("files      5 written, 0 unchanged, 0 removed (listed in "
                      ".txray-export.json)", text)
        self.assertIn("context-layer index <vault> (optional; TimelineXray does not run it)",
                      text)


class ManifestTest(ExportTestCase):
    def test_reexport_removes_only_files_it_created(self) -> None:
        self.ok("--include-stale")
        own = self.out / "my-own-note.md"
        own.write_text("# Mine\n", "utf-8")
        stray = self.out / "txray-findings" / "unrelated.md"
        stray.write_text("# Not an export\n", "utf-8")
        sibling = self.out.parent / "vault-note.md"
        sibling.write_text("# Vault note\n", "utf-8")
        data = self.ok()
        self.assertEqual(sorted(data["files"]["removed"]),
                         sorted([note_path("stale"), note_path("unchecked")]))
        self.assertFalse((self.out / note_path("stale")).exists())
        for path in (own, stray, sibling):
            self.assertTrue(path.exists(), path)
        manifest = json.loads((self.out / MANIFEST).read_text("utf-8"))
        self.assertNotIn(note_path("stale"), manifest["files"])
        self.assertNotIn("pending", manifest)

    def test_an_interrupted_export_can_be_rerun(self) -> None:
        self.ok()
        real = writer_module.atomic_write
        calls = []

        def failing(path: Path, data: bytes) -> None:
            calls.append(path.name)
            if len(calls) == 3:
                raise OSError(28, "No space left on device")
            real(path, data)

        with mock.patch.object(writer_module, "atomic_write", failing):
            code, _, err = export(self.out, "--include-stale")
        self.assertEqual(code, 1)
        self.assertIn(b"No space left", err)
        self.assertIn("pending", json.loads((self.out / MANIFEST).read_text("utf-8")))
        self.ok("--include-stale")
        clean = self.tmp / "vault" / "clean"
        self.ok("--include-stale", out=clean)
        self.assertEqual(tree(self.out), tree(clean))

    def test_an_edited_note_is_never_overwritten_or_removed(self) -> None:
        self.ok("--include-stale")
        edited = self.out / note_path("stale")
        edited.write_text(edited.read_text("utf-8") + "\nMy annotation.\n", "utf-8")
        before = tree(self.out)
        code, _, err = export(self.out)
        self.assertEqual(code, 1)
        self.assertIn(b"was changed after the last export", err)
        self.assertEqual(tree(self.out), before)

    def test_a_file_it_did_not_create_is_refused(self) -> None:
        self.out.mkdir()
        (self.out / INDEX).write_text("# My own index\n", "utf-8")
        before = tree(self.out)
        code, _, err = export(self.out)
        self.assertEqual(code, 1)
        self.assertIn(b"was not created by a TimelineXray export", err)
        self.assertEqual(tree(self.out), before)

    def test_a_tampered_manifest_is_refused(self) -> None:
        self.ok()
        victim = self.out.parent / "victim.md"
        victim.write_text("# keep me\n", "utf-8")
        mine = self.out / "mine.md"
        mine.write_text("# keep me too\n", "utf-8")
        digest = hashlib.sha256(b"# keep me\n").hexdigest()
        for files in ({"../victim.md": digest}, {"mine.md": digest},
                      {"txray-findings/../../victim.md": digest}, "not a dict"):
            with self.subTest(files=files):
                manifest = {"schema": writer_module.MANIFEST_SCHEMA, "files": files}
                (self.out / MANIFEST).write_text(json.dumps(manifest), "utf-8")
                code, _, err = export(self.out)
                self.assertEqual(code, 1)
                self.assertIn(b"not a valid TimelineXray export manifest", err)
                self.assertTrue(victim.exists() and mine.exists())
        (self.out / MANIFEST).write_text("{", "utf-8")
        code, _, err = export(self.out)
        self.assertEqual((code, b"not JSON" in err), (1, True))


class RefusalTest(ExportTestCase):
    def assert_refused(self, out: Path, message: bytes, *, ledger: Path | None = None,
                       watch: Path | None = None) -> None:
        watched = watch or self.tmp
        before = tree(watched)
        code, stdout, err = export(out, ledger=ledger)
        self.assertEqual(code, 1, (stdout, err))
        self.assertIn(message, err)
        self.assertEqual(tree(watched), before)

    def test_destinations_that_are_refused(self) -> None:
        store, ledger = REPO.store_dir, LEDGER
        cases = [
            ("inside the working tree", REPO_ROOT / "txray-export-probe",
             b"inside the TimelineXray working tree"),
            ("inside the package", REPO_ROOT / "src" / "timelinexray" / "notes",
             b"inside the TimelineXray working tree"),
            ("inside the store", store / "notes", b"inside the snapshot store"),
            ("the store itself", store, b"inside the snapshot store"),
            ("inside the ledger", ledger / "notes", b"inside the findings ledger"),
            ("containing the store and ledger", REPO.root, b"contains the snapshot store"),
            ("parent missing", self.tmp / "missing" / "notes", b"does not exist"),
        ]
        for name, out, message in cases:
            with self.subTest(name):
                existed = out.exists()
                inside_repo = REPO.root == out or REPO.root in out.parents
                watch = REPO.root if inside_repo else self.tmp
                before = None if REPO_ROOT in out.parents else tree(watch)
                code, _, err = export(out)
                self.assertEqual(code, 1, err)
                self.assertIn(message, err)
                self.assertEqual(out.exists(), existed)
                if before is not None:
                    self.assertEqual(tree(watch), before)
        afile = self.tmp / "vault" / "a-file"
        afile.write_text("x", "utf-8")
        self.assert_refused(afile, b"is not a directory")

    def test_symbolic_links_are_refused(self) -> None:
        real = self.tmp / "real"
        real.mkdir()
        link = self.tmp / "vault" / "link"
        link.symlink_to(real, target_is_directory=True)
        self.assert_refused(link, b"is a symbolic link")
        self.assertEqual(tree(real), {})

        self.out.mkdir()
        (self.out / "txray-findings").symlink_to(real, target_is_directory=True)
        self.assert_refused(self.out, b"is a symbolic link")
        (self.out / "txray-findings").unlink()

        target = self.tmp / "elsewhere.md"
        target.write_text("# elsewhere\n", "utf-8")
        (self.out / INDEX).symlink_to(target)
        self.assert_refused(self.out, b"is a symbolic link")
        self.assertEqual(target.read_text("utf-8"), "# elsewhere\n")
        (self.out / INDEX).unlink()

        self.ok()
        (self.out / MANIFEST).unlink()
        (self.out / MANIFEST).symlink_to(target)
        self.assert_refused(self.out, b"is a symbolic link")

    def test_ledger_problems_write_nothing(self) -> None:
        missing = self.tmp / "no-ledger"
        self.assert_refused(self.out, b"no findings ledger exists", ledger=missing)
        damaged = self.tmp / "damaged"
        shutil.copytree(LEDGER, damaged)
        events = damaged / "events.jsonl"
        events.write_bytes(events.read_bytes().replace(b'"title":"Filter', b'"title":"Fixer', 1))
        before = tree(damaged)
        code, _, err = export(self.out, ledger=damaged)
        self.assertEqual(code, 1)
        self.assertIn(b"fails verification", err)
        self.assertFalse(self.out.exists())
        self.assertEqual(tree(damaged), before)  # the ledger is read, never written


class AnalyticsTest(ExportTestCase):
    """No analytics data is ever exported, and no export goes near a dataset."""

    def test_no_analytics_data_is_exported(self) -> None:
        from tests.analytics_support import post, run_txray, write_export

        canary = "CANARY" + "-EXPORT-" + "5e2b8d"
        csv = write_export(self.tmp / "export.csv",
                           [post("1000000000000000009", text=canary, impressions=10, likes=1)])
        dataset = self.tmp / "private" / "dataset"
        dataset.parent.mkdir()
        proc = run_txray(["metrics", "import", str(csv), "--schema", "x-post-v1", "--lang",
                          "en", "--out", str(dataset), "--captured-at", "2026-09-30T12:00:00Z",
                          "--store", str(REPO.store_dir)])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assert_refused_export(dataset / "notes", b"lies inside an analytics dataset")
        self.assert_refused_export(dataset.parent, b"contains an analytics dataset")
        ledger_in_dataset = dataset / "ledger"
        shutil.copytree(LEDGER, ledger_in_dataset)
        code, _, err = export(self.out, ledger=ledger_in_dataset)
        self.assertEqual(code, 1)
        self.assertIn(b"analytics dataset lies in or above the findings ledger", err)
        self.assertFalse(self.out.exists())
        code, stdout, stderr = export(self.out, "--include-stale")
        self.assertEqual(code, 0, stderr)
        written = b"".join(tree(self.out).values()) + stdout + stderr
        self.assertNotIn(canary.encode(), written)
        self.assertNotIn(b"analytics-dataset", written)

    def assert_refused_export(self, out: Path, message: bytes) -> None:
        before = tree(self.tmp / "private")
        code, _, err = export(out)
        self.assertEqual(code, 1, err)
        self.assertIn(message, err)
        self.assertEqual(tree(self.tmp / "private"), before)


class IndependenceTest(unittest.TestCase):
    """Context Layer stays optional: never imported, never a dependency, never on by default."""

    def _python_files(self) -> list[Path]:
        return sorted(path for folder in ("src", "tests", "scripts")
                      for path in (REPO_ROOT / folder).rglob("*.py"))

    def test_no_module_imports_context_layer(self) -> None:
        dynamic = {"import_module", "__import__", "find_spec", "run_module"}
        for path in self._python_files():
            tree_ = ast.parse(path.read_text("utf-8"), str(path))
            for node in ast.walk(tree_):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module or ""]
                elif isinstance(node, ast.Call):
                    func = node.func
                    called = func.attr if isinstance(func, ast.Attribute) else getattr(
                        func, "id", "")
                    if called in dynamic:
                        names = [arg.value for arg in node.args
                                 if isinstance(arg, ast.Constant) and isinstance(arg.value, str)]
                for name in names:
                    with self.subTest(file=str(path.relative_to(REPO_ROOT)), name=name):
                        self.assertFalse(name.replace("-", "_").split(".")[0] == "context_layer")

    def test_context_layer_is_not_a_dependency(self) -> None:
        project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text("utf-8"))["project"]
        self.assertEqual(project["dependencies"], [])
        self.assertNotIn("optional-dependencies", project)
        text = (REPO_ROOT / "pyproject.toml").read_text("utf-8").lower()
        self.assertNotIn("context-layer", text)
        self.assertNotIn("context_layer", text)

    def test_nothing_is_enabled_by_default(self) -> None:
        code = ("import sys, timelinexray.cli as c; c.build_parser(); "
                "import timelinexray.mcp.tools as t; "
                "print(sorted(m for m in sys.modules "
                "if m == 'timelinexray.export' or m.startswith('timelinexray.export.')))")
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, env=env,
                              timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), b"[]")
        tools = json.loads((REPO_ROOT / "tests" / "mcp_tools_list.json").read_text("utf-8"))
        names = [tool["name"] for tool in tools["tools"]]
        self.assertEqual(len(names), 12)
        self.assertFalse(any("export" in name or "context" in name for name in names))


if __name__ == "__main__":
    unittest.main()
