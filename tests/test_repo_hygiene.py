"""Repository rules that tests can check mechanically."""

from __future__ import annotations

import hashlib
import re
import tomllib
import unittest
from pathlib import Path

from timelinexray import __version__
from tests.support import REPO_ROOT

DISCLAIMER = (
    "Independent community analysis of publicly available source code. "
    "Not affiliated with or endorsed by X or xAI."
)
APACHE_2_0_SHA256 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"


def _repo_files() -> list[Path]:
    """Files of the working tree, without git metadata, caches and build output."""
    skip = {".git", "__pycache__", "build", "dist"}
    files = []
    for path in sorted(REPO_ROOT.rglob("*")):
        parts = path.relative_to(REPO_ROOT).parts
        if not path.is_file() or skip.intersection(parts):
            continue
        if any(part.endswith(".egg-info") for part in parts) or path.suffix == ".pyc":
            continue
        files.append(path)
    return files


#: Sentences every agent contract file must carry verbatim (whitespace-normalised).
CONTRACT_SENTENCES = (
    "Present a finding as current only when `current` is `true`; when `freshness.newer_pins` "
    "is not empty, run `txray findings reanchor --latest` (or check it without writing: "
    "`verify_claim` with `target_commit`) before relying on it.",
    "Public defaults are not production values: write \"public default\" and the commit next "
    "to every number taken from the code.",
    "A digest item is a mechanical classification, not a finding; review it before citing it "
    "as a claim.",
    "`verify_claim` checks span integrity only: it never establishes truth and it writes "
    "nothing.",
)
CONTRACT_FILES = ("AGENTS.md", "CLAUDE.md", "docs/agents/AGENTS-snippet.md")


class AgentContractTest(unittest.TestCase):
    """FA-024: the agent contract names the re-anchoring step, the public-default wording,
    the digest-item caveat and the integrity-only meaning of verify_claim, in the same words
    in every file an agent may load."""

    def test_contract_sentences_are_present_in_every_contract_file(self) -> None:
        for name in CONTRACT_FILES:
            text = " ".join((REPO_ROOT / name).read_text("utf-8").split())
            for sentence in CONTRACT_SENTENCES:
                with self.subTest(file=name, sentence=sentence[:40]):
                    self.assertIn(sentence, text)


class RepoHygieneTest(unittest.TestCase):
    def test_agents_and_claude_are_identical(self) -> None:
        self.assertEqual((REPO_ROOT / "AGENTS.md").read_bytes(),
                         (REPO_ROOT / "CLAUDE.md").read_bytes())

    def test_disclaimer_is_stated(self) -> None:
        for name in ("README.md", "NOTICE", "AGENTS.md"):
            with self.subTest(name):
                self.assertIn(DISCLAIMER, (REPO_ROOT / name).read_text("utf-8"))

    def test_license_is_the_full_apache_2_0_text(self) -> None:
        data = (REPO_ROOT / "LICENSE").read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), APACHE_2_0_SHA256)

    def test_versions_agree(self) -> None:
        project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text("utf-8"))["project"]
        self.assertEqual(project["version"], __version__)
        self.assertEqual(project["dependencies"], [])
        self.assertEqual(project["scripts"]["txray"], "timelinexray.cli:main")
        changelog = (REPO_ROOT / "CHANGELOG.md").read_text("utf-8")
        first = re.search(r"^## \[([^\]]+)\]", changelog, re.MULTILINE)
        self.assertEqual(first.group(1), __version__)

    def test_no_personal_absolute_paths(self) -> None:
        patterns = [re.compile(p.encode()) for p in ("/" + "Users/[A-Za-z]", "/" + "home/[a-z]")]
        for path in _repo_files():
            data = path.read_bytes()
            for pattern in patterns:
                with self.subTest(path=str(path.relative_to(REPO_ROOT))):
                    self.assertIsNone(pattern.search(data))

    def test_no_secrets_or_personal_contact_data(self) -> None:
        """No credentials, API keys, tokens, private keys or personal e-mail addresses.

        The patterns are split so that this file does not match itself.
        """
        secret_patterns = [re.compile(p.encode()) for p in (
            "gh" + "p_[A-Za-z0-9]{20,}",
            "github" + "_pat_[A-Za-z0-9_]{20,}",
            "gh[ousr]" + "_[A-Za-z0-9]{20,}",
            "s" + "k-[A-Za-z0-9_-]{20,}",
            "AK" + "IA[0-9A-Z]{16}",
            "xo" + "x[baprs]-[A-Za-z0-9-]{10,}",
            "AI" + "za[0-9A-Za-z_-]{30,}",
            "-----BEGIN [A-Z ]*" + "PRIVATE KEY-----",
            "[Bb]earer" + " [A-Za-z0-9._~+/-]{20,}",
            "(?i)(api[_-]?key|secret|passw(or)?d|token)" + "\\s*[:=]\\s*['\"][^'\"\\s]{8,}['\"]",
        )]
        email = re.compile(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
        allowed_email = re.compile(
            rb"@(users\.noreply\.github\.com|github\.com|[A-Za-z0-9.-]*example(\.[a-z]+)?|"
            rb"[A-Za-z0-9.-]+\.(invalid|test|example|localhost))$")
        for path in _repo_files():
            data = path.read_bytes()
            rel = str(path.relative_to(REPO_ROOT))
            for pattern in secret_patterns:
                with self.subTest(path=rel, pattern=pattern.pattern[:12]):
                    self.assertIsNone(pattern.search(data))
            for match in email.finditer(data):
                with self.subTest(path=rel, email=match.group(0)[-24:]):
                    self.assertIsNotNone(allowed_email.search(match.group(0)))

    def test_repository_text_is_english(self) -> None:
        turkish_only = re.compile("[\u011e\u011f\u0130\u0131\u015e\u015f]")
        for path in _repo_files():
            with self.subTest(path=str(path.relative_to(REPO_ROOT))):
                text = path.read_bytes().decode("utf-8", "replace")
                self.assertIsNone(turkish_only.search(text))

    def test_no_upstream_source_is_vendored(self) -> None:
        for path in _repo_files():
            self.assertNotIn(path.suffix, (".scala", ".rs", ".java"),
                             f"unexpected native source file {path}")


class CommunityFilesTest(unittest.TestCase):
    """Issue forms, contact links, the code of conduct and the project URLs."""

    ISSUES = "https://github.com/solisolsoli/timelinexray"

    def _yaml(self, name: str):
        from timelinexray.findings.yamlsub import load

        return load((REPO_ROOT / ".github" / "ISSUE_TEMPLATE" / name).read_text("utf-8"))

    def test_issue_forms_replace_the_markdown_templates(self) -> None:
        directory = REPO_ROOT / ".github" / "ISSUE_TEMPLATE"
        self.assertEqual(sorted(p.name for p in directory.iterdir()),
                         ["bug_report.yml", "config.yml", "feature_request.yml",
                          "wrong_citation.yml"])
        for name, label in (("bug_report.yml", "bug"), ("wrong_citation.yml", "evidence"),
                            ("feature_request.yml", "enhancement")):
            with self.subTest(form=name):
                form = self._yaml(name)  # the project's strict reader: no block scalars
                self.assertEqual(form["labels"], [label])
                self.assertTrue(form["name"] and form["description"])
                fields = [item for item in form["body"] if item["type"] != "markdown"]
                ids = [item["id"] for item in fields]
                self.assertEqual(len(ids), len(set(ids)))
                self.assertTrue(any(item.get("validations", {}).get("required") == "true"
                                    for item in fields), "a form needs a required field")
                for item in fields:
                    self.assertTrue(item["attributes"]["label"])

    def test_config_disables_blank_issues_and_links_security_and_discussions(self) -> None:
        config = self._yaml("config.yml")
        self.assertEqual(config["blank_issues_enabled"], "false")
        urls = [link["url"] for link in config["contact_links"]]
        self.assertEqual(urls, [f"{self.ISSUES}/security/advisories/new",
                                f"{self.ISSUES}/discussions"])

    def test_the_workflow_checker_reads_only_the_workflows_directory(self) -> None:
        text = (REPO_ROOT / "scripts" / "check_workflows.py").read_text("utf-8")
        self.assertIn('(root / ".github" / "workflows").glob("*.y*ml")', text)

    def test_code_of_conduct_is_the_covenant_without_a_personal_contact(self) -> None:
        text = (REPO_ROOT / "CODE_OF_CONDUCT.md").read_text("utf-8")
        self.assertIn("# Contributor Covenant Code of Conduct", text)
        self.assertIn("version 2.1", text)
        self.assertNotIn("[INSERT", text)
        self.assertNotIn("@", text)
        self.assertIn(f"{self.ISSUES}/security/advisories/new", text)
        self.assertIn("report-content flow", text)

    def test_project_urls_follow_the_classifiers(self) -> None:
        raw = (REPO_ROOT / "pyproject.toml").read_text("utf-8")
        self.assertLess(raw.index("classifiers"), raw.index("[project.urls]"))
        urls = tomllib.loads(raw)["project"]["urls"]
        self.assertEqual(list(urls), ["Homepage", "Documentation", "Issues", "Changelog",
                                      "Source", "Security"])
        self.assertEqual(urls["Source"], self.ISSUES)
        self.assertEqual(urls["Security"], f"{self.ISSUES}/security/policy")


class DocsConsistencyTest(unittest.TestCase):
    """Documentation statements that drifted from the code (audit section 6, item 14)."""

    def test_the_goldens_readme_names_the_current_anchor_verdicts(self) -> None:
        from timelinexray.span import FOUND, FOUND_MULTIPLE, MISSING

        text = (REPO_ROOT / "goldens" / "README.md").read_text("utf-8")
        self.assertNotIn("AMBIGUOUS", text)  # renamed FOUND_MULTIPLE in 0.6.0 (FA-014)
        self.assertIn(f"(`{FOUND}`, `{FOUND_MULTIPLE}`, `{MISSING}`", text)

    def test_no_document_names_a_version_after_the_current_one(self) -> None:
        current = tuple(int(part) for part in __version__.split(".")[:3])
        files = [REPO_ROOT / "AGENTS.md", REPO_ROOT / "goldens" / "README.md",
                 *sorted((REPO_ROOT / "docs").glob("*.md")),
                 *sorted((REPO_ROOT / "src").rglob("*.py"))]
        for path in files:
            text = path.read_text("utf-8")
            for match in re.finditer(r"\b0\.([0-9]+)\.([0-9]+)\b", text):
                with self.subTest(path=str(path.relative_to(REPO_ROOT)), version=match[0]):
                    self.assertLessEqual((0, int(match[1]), int(match[2])), current)


if __name__ == "__main__":
    unittest.main()
