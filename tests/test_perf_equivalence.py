"""Speed-ups that must not change a byte: each fast path equals the plain computation."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from timelinexray import cli
from timelinexray.diff import analysis
from timelinexray.errors import IntegrityError, InvalidInput
from timelinexray.index import SYMBOLS_MAX_LIMIT, CodeIndex, glob_literal
from timelinexray.index.schema import database_path
from timelinexray.netguard import Allowlist
from timelinexray.snapshot import SnapshotStore
from timelinexray.syntax import lexical
from tests.diff_support import commit, init_bare, set_branch
from tests.index_support import read_blobs_oracle
from tests.support import REPO_ROOT, UPSTREAM_COMMIT, file_url, git_env, run_cli, upstream_git_dir

UPSTREAM = upstream_git_dir()

SNIPPETS: dict[str, list[str]] = {
    "rust": [
        'let a = r#"raw "quoted" // not a comment"#; let b = br"bytes\\"; let c = cr"c";\n',
        "fn f<'a>(x: &'a str) -> &'a str { 'outer: loop { break 'outer; } x }\n",
        "let c = 'x'; let d = '\\n'; let e = b'\\xff'; let f = '\\u{1F600}'; let g = '\"';\n",
        "/* outer /* nested */ still outer */ fn g() {} // tail\n",
        'let s = b"bytes"; let t = c"cstr"; let u = "esc \\" quote"; let v = ident"x";\n',
        'let r = r##"a "# b"##; // x\n',
        'let unterminated = "abc\nfn after() {}\n',
        "let raw_open = r#\"never closed\n",
        "/* never closed\n",
        "\ufeff// bom first\nfn h() {}\n",
        "let br = 1; let bc = 2; let c2 = rb; let x = bar'a';\n",
        "",
        "no tokens at all here 1 + 2\n",
    ],
    "scala": [
        'val s = s"hi ${name} and ${"nested"}"; val t = """tri "quoted" """"; // c\n',
        "val c = 'x'; val d = '\\n'; val e = '\\u0041'; val sym = 'sym\n",
        "/* a /* nested */ b */ object O { def f = 1 }\n",
        'val u = "unterminated\nval next = 1\n',
        'val i = f"$a%d" + raw"\\n" + "esc \\" q"\n',
        'val p = """never closed\n',
        "/* never closed\n",
        'val x = s"${ "}" }" // tricky\n',
    ],
    "java": [
        'String t = """\n  text "block" \\""" end\n  """; // c\n',
        "char c = 'x'; char d = '\\n'; char e = '\\u0041'; char f = '\\177';\n",
        "/* block */ class A { /* c */ int x; } // line\n",
        'String s = "esc \\" quote"; String u = "unterminated\nint y;\n',
        'String z = """never closed\n',
        "/* never closed\n",
        "@Ann(\"v\") public void f() { a = 'q'; }\n",
    ],
    "python": [
        'a = f"{x!r}" + rb"raw\\" + Rb\'x\' + u"u" + BR"z" + fr\'{y}\'\n',
        's = """doc "quoted" \\""" tail"""  # comment\n',
        "t = '''a\n b''' ; u = 'it\\'s'\n",
        'bad = "unterminated\nok = 1  # c\n',
        'rr = rrr"x"; ff = fff"y"; xr = xr"z"\n',
        'v = """never closed\n',
        "name = 'a' 'b'  # '#' inside\n",
        "# only a comment",
        "x = 1 + 2\n",
        "\ufeffprint('bom')\n",
    ],
}


def mask_plain(text: str, language: str) -> lexical.Masked:
    """``mask`` with the unguarded token patterns."""
    with mock.patch.dict(lexical._TOKENS, lexical._PLAIN_TOKENS):  # noqa: SLF001
        return lexical.mask(text, language)


def random_text(language: str, rng: random.Random) -> str:
    alphabet = list(SNIPPETS[language][0]) + list("rbcfuRBUF'\"#/*\\ \n{}()$a1\u00e9")
    return "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 120)))


class GuardedMaskTests(unittest.TestCase):
    def test_guard_is_a_prefix_of_every_alternative(self) -> None:
        self.assertEqual(set(lexical._TOKENS), set(lexical._PLAIN_TOKENS))  # noqa: SLF001
        self.assertNotEqual(
            lexical._TOKENS["rust"].pattern, lexical._PLAIN_TOKENS["rust"].pattern  # noqa: SLF001
        )

    def test_snippets(self) -> None:
        for language, snippets in SNIPPETS.items():
            for snippet in snippets:
                with self.subTest(language=language, snippet=snippet):
                    self.assertEqual(lexical.mask(snippet, language), mask_plain(snippet, language))

    def test_random_texts(self) -> None:
        rng = random.Random(20260801)
        for language in SNIPPETS:
            for _ in range(400):
                text = random_text(language, rng)
                self.assertEqual(lexical.mask(text, language), mask_plain(text, language), text)

    def test_every_first_character_is_listed(self) -> None:
        """Each character a plain match can start with is in the guard set."""
        for language, plain in lexical._PLAIN_TOKENS.items():  # noqa: SLF001
            first = lexical._TOKEN_FIRST[language]  # noqa: SLF001
            for snippet in SNIPPETS[language]:
                for match in plain.finditer(snippet):
                    self.assertIn(snippet[match.start()], first, (language, snippet))


@unittest.skipIf(UPSTREAM is None, "no local x-algorithm clone with 77d431a "
                 "(set TXRAY_TEST_UPSTREAM to a local clone)")
class GuardedMaskUpstreamTests(unittest.TestCase):
    def test_mask_equal_on_upstream_files(self) -> None:
        extensions = {".rs": "rust", ".scala": "scala", ".java": "java", ".py": "python"}
        listing = subprocess.run(
            ["git", f"--git-dir={UPSTREAM}", "-c", "protocol.allow=never", "ls-tree", "-r", "-z",
             UPSTREAM_COMMIT],
            capture_output=True, check=True, env=git_env(),
        ).stdout
        wanted: list[tuple[str, str, str]] = []
        for record in listing.split(b"\0"):
            if not record:
                continue
            meta, _, raw = record.partition(b"\t")
            mode, kind, oid = meta.decode("ascii").split()
            path = raw.decode("utf-8", "surrogateescape")
            language = extensions.get(path[path.rfind("."):]) if "." in path else None
            if language is not None and kind == "blob" and mode != "120000":
                wanted.append((path, language, oid))
        blobs = read_blobs_oracle(UPSTREAM, [oid for _, _, oid in wanted])
        checked = 0
        for path, language, oid in wanted:
            try:
                text = blobs[oid].decode("utf-8")
            except UnicodeDecodeError:
                continue
            self.assertEqual(lexical.mask(text, language), mask_plain(text, language), path)
            checked += 1
        self.assertGreater(checked, 100)


class ComponentPrefilterTests(unittest.TestCase):
    """``_may_name_component`` is a superset of ``_COMPONENT_WORD.search``."""

    def check(self, text: str) -> bool:
        matches = analysis._COMPONENT_WORD.search(text) is not None  # noqa: SLF001
        may = analysis._may_name_component(text)  # noqa: SLF001
        if matches:
            self.assertTrue(may, text)
        return matches

    def test_word_lists_are_the_words_of_the_pattern(self) -> None:
        pattern = analysis._COMPONENT_WORD.pattern  # noqa: SLF001
        suffix_group = re.search(r"\(\?:([A-Za-z|]+)\)", pattern)
        plural_group = re.search(r"\(\?i:([A-Za-z_?|]+)\)", pattern)
        assert suffix_group and plural_group
        self.assertEqual(set(analysis._COMPONENT_SUFFIXES), set(suffix_group[1].split("|")))  # noqa: SLF001
        plurals: set[str] = set()
        for word in plural_group[1].split("|"):
            if "_?" in word:
                plurals.update({word.replace("_?", ""), word.replace("_?", "_")})
            else:
                plurals.add(word)
        self.assertEqual(set(analysis._COMPONENT_PLURALS), plurals)  # noqa: SLF001

    def test_curated_texts(self) -> None:
        texts = [
            "", "nothing here", "struct FooFilter;", "let filters = vec![];",
            "const SIDEEFFECTS: [u8; 0] = [];", "let side_effects = 1;", "SIDE_EFFECTS",
            "fooScorer", "Scorers", "scorer", "lowercasefilter", "PIPELINES", "Handlers",
            "r\u00e9sum\u00e9 filters", "FILTERS\u00e9", "fi\u0307lters", "F\u0130LTERS", "filter\u017f",
            "\u017fources", "RULE\u017f", "\ufb01lters", "KILLRULES", "\u212aules",
        ]
        for text in texts:
            self.check(text)

    def test_generated_texts(self) -> None:
        rng = random.Random(20260802)
        words = list(analysis._COMPONENT_SUFFIXES) + list(analysis._COMPONENT_PLURALS)  # noqa: SLF001
        words += ["x", "Foo", "bar_", "filter", "Rul", "ule", "S", "s", "\u017f", "\u0130", "\n", " "]
        hits = 0
        for _ in range(4000):
            text = "".join(rng.choice(words).swapcase() if rng.random() < 0.3 else rng.choice(words)
                           for _ in range(rng.randrange(0, 6)))
            hits += self.check(text)
        self.assertGreater(hits, 500)


@unittest.skipIf(UPSTREAM is None, "no local x-algorithm clone with 77d431a "
                 "(set TXRAY_TEST_UPSTREAM to a local clone)")
class ComponentPrefilterUpstreamTests(unittest.TestCase):
    def test_superset_on_upstream_texts(self) -> None:
        listing = subprocess.run(
            ["git", f"--git-dir={UPSTREAM}", "-c", "protocol.allow=never", "ls-tree", "-r",
             UPSTREAM_COMMIT],
            capture_output=True, check=True, env=git_env(),
        ).stdout.decode("utf-8", "surrogateescape")
        oids = [line.split()[2] for line in listing.splitlines() if line.split()[1] == "blob"]
        blobs = read_blobs_oracle(UPSTREAM, oids)
        matches = rejected = 0
        for blob in blobs.values():
            text = blob.decode("utf-8", "replace")
            if analysis._COMPONENT_WORD.search(text):  # noqa: SLF001
                matches += 1
                self.assertTrue(analysis._may_name_component(text))  # noqa: SLF001
            elif not analysis._may_name_component(text):  # noqa: SLF001
                rejected += 1
        self.assertGreater(matches, 0)
        self.assertGreater(rejected, 0)


RUST_TWO = b"pub fn one() -> u32 { two() }\npub fn two() -> u32 { 2 }\nconst W: f64 = 0.5;\n"
SESSION_FILES = {
    "src/caf\udce9.rs": RUST_TWO,  # b"caf\xe9.rs", not UTF-8
    "src/we*ird[1]?.rs": RUST_TWO + b"pub fn three() {}\n",
    "src/plain.py": b"def alpha():\n    return beta()\n\ndef beta():\n    return 1\n",
    "src/Big.java": b"class Big { void run() { go(); } }\n",
    "docs/notes.md": b"# notes\n",
    "src/empty.rs": b"",
}


class ReadSessionTests(unittest.TestCase):
    """``ReadSession.file_symbols`` equals ``CodeIndex.symbols`` on the glob of the path."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="txray-session-")
        root = Path(cls._tmp.name)
        git_dir = init_bare(root / "upstream.git")
        cls.first = commit(git_dir, SESSION_FILES, [], 1)
        cls.second = commit(git_dir, {**SESSION_FILES, "src/plain.py": b"def alpha():\n    pass\n"},
                            [cls.first], 2)
        set_branch(git_dir, "main", cls.second)
        url = file_url(git_dir)
        cls.store = SnapshotStore(root / "store")
        for item in (cls.first, cls.second):
            cls.store.pin(item, url, allowlist=Allowlist([url]))
        cls.index = CodeIndex(cls.store)
        cls.index.build(cls.first)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def expected(self, commit_id: str, path: str) -> tuple:
        result = self.index.symbols(commit_id, path_glob=glob_literal(path), limit=SYMBOLS_MAX_LIMIT)
        return tuple(record for record in result.symbols if record.path == path)

    def test_equals_the_glob_query_for_every_path(self) -> None:
        paths = [entry.path for entry in self.store.load_manifest(self.first)[1].entries]
        self.assertIn("src/caf\udce9.rs", paths)
        with self.index.read_session() as session:
            for path in [*paths, "src/absent.rs", "src/we*"]:
                with self.subTest(path=path):
                    self.assertEqual(session.file_symbols(self.first, path),
                                     self.expected(self.first, path))
            self.assertTrue(session.file_symbols(self.first, "src/caf\udce9.rs"))
            self.assertEqual(len(session.file_symbols(self.first, "src/we*ird[1]?.rs")), 4)
            self.assertEqual(session.file_symbols(self.first, "src/we*"), ())

    def test_exact_lookup_takes_any_valid_path_and_refuses_an_unencodable_one(self) -> None:
        with self.index.read_session() as session:
            self.assertEqual(session.file_symbols(self.first[:12], "src/plain.py"),
                             self.expected(self.first, "src/plain.py"))
            # exact byte equality: a path with a control character or of any length is a
            # lookup (here: no such file), not a refused pattern; the glob form still refuses
            for odd in ("src/new\nline.py", "src/a\x01b.rs", "x" * 5000):
                self.assertEqual(session.file_symbols(self.first, odd), ())
                with self.assertRaises(InvalidInput):
                    self.index.symbols(self.first, path_glob=glob_literal(odd))
            with self.assertRaises(InvalidInput):  # not a surrogateescape byte: no bytes exist
                session.file_symbols(self.first, "src/lone\ud800.py")

    def test_generation_check_stays(self) -> None:
        with self.index.read_session() as session:
            with self.assertRaises(Exception) as unindexed:  # commit not indexed yet
                session.file_symbols(self.second, "src/plain.py")
            self.assertEqual(type(unindexed.exception).__name__, "NotFound")
        connection = sqlite3.connect(database_path(self.index.root))
        try:
            original = connection.execute(
                "SELECT manifest_sha256 FROM generations WHERE commit_id = ?", (self.first,)
            ).fetchone()[0]
            connection.execute("UPDATE generations SET manifest_sha256 = ? WHERE commit_id = ?",
                               ("0" * 64, self.first))
            connection.commit()
            with self.index.read_session() as session:
                with self.assertRaises(IntegrityError):
                    session.file_symbols(self.first, "src/plain.py")
            connection.execute("UPDATE generations SET manifest_sha256 = ? WHERE commit_id = ?",
                               (original, self.first))
            connection.commit()
        finally:
            connection.close()

    def test_one_snapshot_while_a_build_commits(self) -> None:
        index = CodeIndex(self.store, Path(self._tmp.name) / "snapshot-index")
        index.build(self.first)
        session = index.read_session()
        before = session.file_symbols(self.first, "src/plain.py")
        index.build(self.second)  # a writer commits while the session is open
        self.assertEqual(session.file_symbols(self.first, "src/plain.py"), before)
        with self.assertRaises(Exception) as late:  # the snapshot predates the second build
            session.file_symbols(self.second, "src/plain.py")
        self.assertEqual(type(late.exception).__name__, "NotFound")
        session.close()
        with index.read_session() as fresh:
            self.assertEqual(len(fresh.file_symbols(self.second, "src/plain.py")), 1)
            self.assertEqual(fresh.file_symbols(self.first, "src/plain.py"), before)

    def test_closing_twice_and_reopening(self) -> None:
        session = self.index.read_session()
        session.close()
        session.close()
        self.assertTrue(session.file_symbols(self.first, "src/plain.py"))
        session.close()


def _leaves(parser: argparse.ArgumentParser, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    groups = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]  # noqa: SLF001
    if not groups:
        return [prefix]
    leaves: list[tuple[str, ...]] = []
    for name, sub in groups[0].choices.items():
        leaves += _leaves(sub, (*prefix, name))
    return leaves


def _top_level_names(parser: argparse.ArgumentParser) -> list[str]:
    for action in parser._actions:  # noqa: SLF001
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            return list(action.choices)
    return []


class LazyParserTests(unittest.TestCase):
    """``build_parser(argv)`` imports one command module; every outcome equals the full parser's."""

    def eager(self, argv: list[str]) -> tuple[int, bytes, bytes]:
        with mock.patch.object(cli, "_modules_for", lambda _argv: tuple(cli._COMMAND_MODULES)):  # noqa: SLF001
            return run_cli(argv)

    def test_the_module_table_is_what_the_modules_register(self) -> None:
        full = _top_level_names(cli.build_parser())
        listed = ["pin", "manifest", "show"]
        for module, names in cli._COMMAND_MODULES.items():  # noqa: SLF001
            parser = argparse.ArgumentParser()
            commands = parser.add_subparsers()
            common = argparse.ArgumentParser(add_help=False)
            importlib.import_module(f"timelinexray.{module}").register(commands, common)
            self.assertEqual(list(commands.choices), list(names), module)
            listed += names
        self.assertEqual(listed, full)
        for name in full:
            self.assertEqual(_top_level_names(cli.build_parser([name])), (
                ["pin", "manifest", "show"] + [n for ns in cli._COMMAND_MODULES.values()  # noqa: SLF001
                                               if name in ns for n in ns]))

    def test_outputs_equal_the_full_parser_byte_for_byte(self) -> None:
        leaves = _leaves(cli.build_parser())
        self.assertGreater(len(leaves), 20)
        battery: list[list[str]] = [
            [], ["--json"], ["--help"], ["-h"], ["--help", "--json"], ["--version"],
            ["--version", "--json"], ["--vers"], ["--he"], ["bogus"], ["bogus", "--json"],
            ["ind"], ["Index"], ["--store", "x", "index"], ["--json", "index"], ["--no-such"],
            ["-x", "--json"], ["findings"], ["findings", "--json"], ["findings", "nope"],
            ["metrics"], ["mcp", "--json"], ["export"], ["export", "nope", "--json"],
            ["show"], ["show", "--json"], ["show", "abcdef1"], ["show", "abcdef1", "a", "--json"],
            ["pin"], ["pin", "--json"], ["manifest", "--json"], ["search", "abcdef1"],
            ["search", "abcdef1", "--json"], ["symbols"], ["param"], ["param-history", "--json"],
            ["diff", "a"], ["digest", "--json"], ["update"], ["update", "--json"],
            ["index", "abcdef1", "--limit"], ["show", "abcdef1", "a", "--lines", "x", "--raw"],
        ]
        for leaf in leaves:
            battery += [[*leaf, "--help"], [*leaf, "-h"], [*leaf, "--help", "--json"],
                        [*leaf, "--no-such-option"], [*leaf, "--json", "--no-such-option"]]
        for argv in battery:
            with self.subTest(argv=argv):
                self.assertEqual(run_cli(argv), self.eager(argv))
        self.assertTrue(any(run_cli(argv)[1] for argv in battery))

    def test_valid_arguments_parse_to_the_same_namespace(self) -> None:
        valid = [
            ["pin", "abcdef1"], ["manifest", "abcdef1", "--summary", "--json"],
            ["show", "abcdef1", "a/b", "--lines", "1-2", "--anchor=x"],
            ["index", "abcdef1", "--rebuild"], ["search", "abcdef1", "q", "--path", "src/*"],
            ["symbols", "abcdef1", "--kind", "function", "--calls"], ["param", "W", "--commit", "abcdef1"],
            ["param-history", "W", "--base", "a", "--head", "b"], ["findings", "list", "--json"],
            ["mcp", "tools", "--json"], ["diff", "a", "b"], ["digest", "a", "b", "--json"],
            ["update", "--out", "d"], ["metrics", "rwe", "data.json", "--json"], ["export", "context-layer", "--out", "d"],
            ["setup", "--no-index"],
        ]
        for argv in valid:
            with self.subTest(argv=argv):
                expected = vars(cli.build_parser().parse_args(argv))
                self.assertEqual(vars(cli.build_parser(argv).parse_args(argv)), expected)
        self.assertTrue(any(
            vars(cli.build_parser(argv).parse_args(argv)).get("handler") for argv in valid[:4]))

    def test_only_the_named_module_is_imported(self) -> None:
        code = (
            "import json, sys, timelinexray.cli as c\n"
            "c.build_parser(sys.argv[1:])\n"
            "print(json.dumps(sorted(m for m in sys.modules if m.endswith('_cli'))))\n"
        )
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}

        def loaded(*argv: str) -> list[str]:
            proc = subprocess.run([sys.executable, "-c", code, *argv], capture_output=True,
                                  env=env, timeout=60, check=True)
            return json.loads(proc.stdout)

        self.assertEqual(loaded("search", "abcdef1", "q"), ["timelinexray.index_cli"])
        self.assertEqual(loaded("diff", "a", "b"), ["timelinexray.digest_cli"])
        self.assertEqual(loaded("show", "a", "b"), [])
        self.assertEqual(loaded("--version"), [])
        everything = sorted(f"timelinexray.{m}" for m in cli._COMMAND_MODULES)  # noqa: SLF001
        for argv in (["--help"], ["bogus"], [], ["--json", "index"]):
            self.assertEqual(loaded(*argv), everything, argv)


if __name__ == "__main__":
    unittest.main()
