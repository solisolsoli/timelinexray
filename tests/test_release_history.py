"""``scripts/check_release_history.py`` on synthetic repositories, and on this one.

The script is a developer tool (not part of the package); it is loaded from its file. Every
repository here is created with ``git init`` in a temporary directory with an isolated
``HOME``, so no user configuration (sign-off, signing, hooks) leaks into the commits.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType

from tests.support import REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "check_release_history.py"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("txray_script_check_release_history", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


history = _script()
IDENTITY = history.IDENTITY
NAME, EMAIL = IDENTITY.split(" <")[0], IDENTITY.split(" <")[1].rstrip(">")


class SyntheticRepo:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "home"
        self.home.mkdir()
        self.path = root / "repo"
        self.path.mkdir()
        self.git("init", "-q", "--initial-branch=main")

    def env(self, **extra: str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update({"HOME": str(self.home), "GIT_CONFIG_NOSYSTEM": "1", "LC_ALL": "C",
                    "GIT_AUTHOR_NAME": NAME, "GIT_AUTHOR_EMAIL": EMAIL,
                    "GIT_COMMITTER_NAME": NAME, "GIT_COMMITTER_EMAIL": EMAIL,
                    "GIT_AUTHOR_DATE": "1700000000 +0000",
                    "GIT_COMMITTER_DATE": "1700000000 +0000"})
        env.update(extra)
        return env

    def git(self, *args: str, **env: str) -> str:
        proc = subprocess.run(["git", "-C", str(self.path), *args], capture_output=True,
                              text=True, env=self.env(**env), timeout=60)
        if proc.returncode != 0:
            raise AssertionError(f"git {args}: {proc.stderr}")
        return proc.stdout

    def commit(self, message: str, **env: str) -> str:
        self.git("-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "--no-verify",
                 "-m", message, **env)
        return self.git("rev-parse", "HEAD").strip()


class CheckReleaseHistoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="txray-history-")
        self.addCleanup(self._tmp.cleanup)
        self.repo = SyntheticRepo(Path(self._tmp.name))

    def run_script(self, *argv: str) -> tuple[int, str, str]:
        proc = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(self.repo.path),
                               *argv], capture_output=True, text=True, timeout=120,
                              env=self.repo.env())
        return proc.returncode, proc.stdout, proc.stderr

    def problems(self, *argv: str) -> list[tuple[str, str]]:
        code, out, err = self.run_script("--json", *argv)
        report = json.loads(out)
        self.assertEqual(code, 0 if report["ok"] else 1, err)
        return [(p["code"], p["message"]) for p in report["problems"]]

    def test_a_clean_history_passes(self) -> None:
        self.repo.commit("Add the thing\n\nTwo paragraphs of prose: with a colon, and a\n"
                         "second line.\n\nMeasured on this machine: 2 s, not promised.")
        self.repo.commit("Second commit")
        code, out, _ = self.run_script()
        self.assertEqual(code, 0, out)
        self.assertIn("release history check: ok (2 commits on HEAD", out)
        self.assertEqual(self.problems(), [])

    def test_time_zone_offsets_must_be_utc(self) -> None:
        self.repo.commit("Author offset", GIT_AUTHOR_DATE="1700000000 +0900")
        self.repo.commit("Committer offset", GIT_COMMITTER_DATE="1700000000 -0500")
        found = self.problems()
        self.assertEqual([code for code, _ in found], ["timezone", "timezone"])
        self.assertIn("committer time zone offset is -0500", found[0][1])
        self.assertIn("author time zone offset is +0900", found[1][1])

    def test_identity_must_be_the_project_identity(self) -> None:
        self.repo.commit("Other name", GIT_AUTHOR_NAME="someone", GIT_COMMITTER_NAME="someone")
        self.repo.commit("Other committer e-mail", GIT_COMMITTER_EMAIL="x@" + "corp-mail.com")
        found = self.problems()
        self.assertEqual([code for code, _ in found], ["identity"] * 3)
        self.assertIn("committer is", found[0][1])
        self.assertIn("author is 'someone <", found[1][1])
        self.assertIn("committer is 'someone <", found[2][1])
        self.assertEqual(self.problems("--identity", "someone <" + EMAIL + ">", "HEAD~1"), [])

    def test_trailers_are_refused(self) -> None:
        self.repo.commit("With a trailer\n\nBody.\n\nCo-Authored-By: Someone <s@example.com>")
        self.repo.commit("Trailer block\n\nBody.\n\nReviewed-by: A <a@example.com>\n"
                         "Change-Id: I0123456789")
        self.repo.commit("Sign-off anywhere\n\nSigned-off-by: A <a@example.com>\n\nMore text.")
        found = self.problems()
        self.assertEqual([code for code, _ in found], ["trailer"] * 4)
        self.assertTrue(any("Co-Authored-By" in m for _, m in found))
        self.assertTrue(any("Change-Id" in m for _, m in found))
        self.assertTrue(any("Signed-off-by" in m for _, m in found))

    def test_personal_data_in_messages_is_refused(self) -> None:
        path = "/" + "Users/someone/project"
        home = "/" + "home/someone/project"
        mail = "someone@" + "corp-mail.com"
        token = "gh" + "p_" + "A" * 24
        self.repo.commit(f"Path\n\nSee {path} and {home}.")
        self.repo.commit(f"Mail\n\nContact {mail}; fine: {EMAIL} and ops@example.org.")
        self.repo.commit(f"Token\n\nUse {token} to log in.")
        found = self.problems()
        codes = [code for code, _ in found]
        self.assertEqual(codes, ["personal_data"] * 3)
        self.assertIn("secret", found[0][1])
        self.assertIn("e-mail address", found[1][1])
        self.assertIn(mail, found[1][1])
        self.assertIn("local path", found[2][1])

    def test_extra_patterns_from_a_file_outside_the_repository(self) -> None:
        patterns = Path(self._tmp.name) / "personal-terms.txt"
        patterns.write_text("# one regular expression per line\nfavourite-cafe\nproject\\s+zeta\n")
        self.repo.commit("Mentions the favourite-cafe build server")
        self.repo.commit("Clean")
        found = self.problems("--patterns", str(patterns))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][0], "personal_data")
        self.assertIn("favourite-cafe", found[0][1])
        self.assertEqual(self.problems("--patterns", str(patterns), "HEAD~1..HEAD"), [])

    def test_a_range_limits_the_commits_checked(self) -> None:
        self.repo.commit("Old offset", GIT_AUTHOR_DATE="1700000000 +0900")
        base = self.repo.commit("Base")
        self.repo.commit("New")
        self.assertEqual(len(self.problems()), 1)
        self.assertEqual(self.problems(f"{base}..HEAD"), [])

    def test_fsck_reports_dangling_objects_and_the_prune_commands(self) -> None:
        self.repo.commit("Kept")
        self.repo.commit("Reset away")
        self.repo.git("reset", "-q", "--hard", "HEAD~1")
        code, out, _ = self.run_script()
        self.assertEqual(code, 0, out)
        code, out, _ = self.run_script("--fsck")
        self.assertEqual(code, 1, out)
        self.assertIn("dangling: dangling commit", out)
        self.assertIn("git reflog expire --expire=now --all && git gc --prune=now", out)
        self.repo.git("reflog", "expire", "--expire=now", "--all")
        self.repo.git("gc", "-q", "--prune=now")
        code, out, _ = self.run_script("--fsck")
        self.assertEqual(code, 0, out)
        self.assertIn("no dangling objects", out)

    def test_usage_and_git_errors(self) -> None:
        code, _, err = self.run_script("no-such-ref")
        self.assertEqual(code, 2)
        self.assertIn("git log", err)
        proc = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(self.repo.home)],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("not a git repository", proc.stderr)
        code, _, err = self.run_script("--", "-x")
        self.assertEqual(code, 2)

    def test_the_rules_as_functions(self) -> None:
        self.assertEqual(history.message_trailers("Subject\n\nBody with a colon: here.\n"), [])
        self.assertEqual(history.message_trailers("Subject: only\n"), [])
        self.assertEqual(history.message_trailers("Subject\n\nAcked-by: X <x@example.com>\n"),
                         ["Acked-by: X <x@example.com>"])
        self.assertEqual(history.personal_data("ops@example.org and a@b.invalid", []), [])
        self.assertEqual(history.personal_data("built by " + "/" + "home/x/y", []),
                         ["local path: '/" + "home/x'"])


class RepositoryHistoryTest(unittest.TestCase):
    """This repository's own history: the identity, trailer and personal-data rules hold
    for every commit (the UTC rule is the release gate, applied to the published branch)."""

    @unittest.skipUnless((REPO_ROOT / ".git").exists(), "not a git checkout")
    def test_identity_trailers_and_messages(self) -> None:
        commits = history.read_commits(REPO_ROOT, "HEAD")
        self.assertTrue(commits)
        problems = [p for p in history.check_commits(commits, IDENTITY, [])
                    if p["code"] != "timezone"]
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
