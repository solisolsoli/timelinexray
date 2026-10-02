"""``fsutil``: where a command may write (``is_within``, ``check_output_directory``) and how
files are replaced (``atomic_write``)."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from timelinexray.errors import Refused
from timelinexray.export.writer import check_destination
from timelinexray.fsutil import atomic_write, check_output_directory, is_within


def _case_insensitive(directory: Path) -> bool:
    """Whether ``directory`` is on a file system that folds case (default APFS)."""
    probe = directory / "CaseProbe"
    probe.mkdir()
    try:
        return (directory / "caseprobe").is_dir()
    finally:
        probe.rmdir()


class IsWithinTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="txray-fsutil-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()

    def test_textual_and_missing_paths(self) -> None:
        root = self.tmp / "root"
        root.mkdir()
        self.assertTrue(is_within(root, root))
        self.assertTrue(is_within(root / "a" / "b", root))  # does not exist yet
        self.assertFalse(is_within(self.tmp, root))
        self.assertFalse(is_within(self.tmp / "other", root))
        self.assertFalse(is_within(root / "a", self.tmp / "absent"))

    def test_another_spelling_of_the_same_directory(self) -> None:
        """A symbolic-link spelling is the same directory to ``os.path.samestat`` (this is the
        platform-independent stand-in for a case-folding spelling: the textual comparison
        sees two unrelated paths)."""
        root = self.tmp / "Store"
        (root / "sub").mkdir(parents=True)
        link = self.tmp / "alias"
        os.symlink(root, link, target_is_directory=True)
        self.assertFalse(link == root or root in link.parents)  # the textual test fails
        self.assertTrue(is_within(link, root))
        self.assertTrue(is_within(link / "sub" / "out", root))
        self.assertFalse(is_within(self.tmp / "elsewhere", root))


class CaseInsensitiveSpellingTest(unittest.TestCase):
    """On a case-insensitive volume ``store`` and ``Store`` are one directory."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="txray-case-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        if not _case_insensitive(self.tmp):
            self.skipTest("optional: the temporary directory is on a case-sensitive file system")

    def test_out_inside_the_store_under_another_spelling_is_refused(self) -> None:
        store = self.tmp / "Store"
        store.mkdir()
        with self.assertRaises(Refused) as raised:
            check_output_directory(self.tmp / "store" / "out", store_root=store)
        self.assertIn("lies inside the snapshot store", str(raised.exception))
        with self.assertRaises(Refused):
            check_output_directory(self.tmp / "STORE", store_root=store)

    def test_out_inside_the_ledger_under_another_spelling_is_refused(self) -> None:
        ledger = self.tmp / "Ledger"
        ledger.mkdir()
        with self.assertRaises(Refused) as raised:
            check_output_directory(self.tmp / "ledger" / "notes", ledger=ledger)
        self.assertIn("lies inside the findings ledger", str(raised.exception))

    def test_export_out_that_contains_the_store_under_another_spelling_is_refused(self) -> None:
        vault = self.tmp / "Vault"
        store, ledger = vault / "store", self.tmp / "ledger"
        store.mkdir(parents=True)
        ledger.mkdir()
        with self.assertRaises(Refused) as raised:
            check_destination(self.tmp / "vault", store_root=store, ledger=ledger)
        self.assertIn("contains the snapshot store", str(raised.exception))

    def test_an_unrelated_spelling_is_still_allowed(self) -> None:
        store = self.tmp / "Store"
        store.mkdir()
        (self.tmp / "other").mkdir()
        self.assertEqual(check_output_directory(self.tmp / "other" / "out", store_root=store),
                         self.tmp / "other" / "out")


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


class AtomicWriteModeTest(unittest.TestCase):
    """A replaced file keeps its permission bits; a new one gets ``0o644`` under the umask
    (``mkstemp`` would have made every file ``0o600``; ``0o666`` would let a permissive umask
    make store and ledger files writable by others)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="txray-atomic-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def with_umask(self, mask: int) -> None:
        previous = os.umask(mask)
        self.addCleanup(os.umask, previous)

    def test_a_new_file_gets_the_umask_default(self) -> None:
        for mask, expected in ((0o022, 0o644), (0o002, 0o644), (0o000, 0o644), (0o077, 0o600)):
            with self.subTest(umask=oct(mask)):
                self.with_umask(mask)
                target = self.tmp / f"new-{mask:o}" / "file.txt"
                atomic_write(target, b"data")
                self.assertEqual((target.read_bytes(), _mode(target)), (b"data", expected))

    def test_an_existing_file_keeps_its_mode(self) -> None:
        self.with_umask(0o022)
        for mode in (0o640, 0o600, 0o755, 0o664):
            with self.subTest(mode=oct(mode)):
                target = self.tmp / f"old-{mode:o}"
                target.write_bytes(b"old")
                target.chmod(mode)
                atomic_write(target, b"new")
                self.assertEqual((target.read_bytes(), _mode(target)), (b"new", mode))

    def test_a_symbolic_link_is_replaced_and_its_target_never_followed(self) -> None:
        self.with_umask(0o022)
        elsewhere = self.tmp / "elsewhere"
        elsewhere.write_bytes(b"keep")
        elsewhere.chmod(0o600)
        link = self.tmp / "link"
        link.symlink_to(elsewhere)
        atomic_write(link, b"new")
        self.assertFalse(link.is_symlink())
        self.assertEqual((link.read_bytes(), _mode(link)), (b"new", 0o644))
        self.assertEqual((elsewhere.read_bytes(), _mode(elsewhere)), (b"keep", 0o600))

    def test_no_temporary_file_is_left_behind(self) -> None:
        target = self.tmp / "file.txt"
        atomic_write(target, b"one")
        atomic_write(target, b"two")
        self.assertEqual([path.name for path in self.tmp.iterdir()], ["file.txt"])
        with self.assertRaises(IsADirectoryError):
            atomic_write(self.tmp, b"x")  # replacing a directory fails; nothing is left over
        self.assertEqual(sorted(path.name for path in self.tmp.iterdir()), ["file.txt"])


if __name__ == "__main__":
    unittest.main()
