"""Synthetic multi-commit fixture histories for the diff, digest and update tests.

Every repository is built from git objects only (``hash-object``, ``mktree``,
``commit-tree``) with a fixed identity and one committer date per commit, so commit ids
are the same on every run. Commit messages are deliberately misleading: the tests check
that no digest ever repeats them.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from timelinexray.gitio import git_env
from tests.support import FIXTURE_IDENTITY

#: A message no digest may repeat (commit messages are never evidence).
MISLEADING_MESSAGE = "Set ClickWeight to 12345.678 and remove every filter"


def run_git(git_dir: Path, *args: str, input: bytes | None = None, date: str | None = None) -> str:
    env = {**git_env(), **FIXTURE_IDENTITY}
    if date is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
    proc = subprocess.run(
        ["git", f"--git-dir={git_dir}", "-c", "protocol.allow=never", *args],
        input=input, capture_output=True, env=env,
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {args} failed: {proc.stderr.decode('utf-8', 'replace')}")
    return proc.stdout.decode("utf-8", "surrogateescape").strip()


def init_bare(git_dir: Path) -> Path:
    subprocess.run(
        ["git", "init", "--bare", "--quiet", "--template=", "--initial-branch=main", str(git_dir)],
        check=True, env=git_env(),
    )
    return git_dir


def write_tree(git_dir: Path, files: Mapping[str, bytes]) -> str:
    root: dict[str, object] = {}
    for path, content in files.items():
        node = root
        *parents, name = path.split("/")
        for part in parents:
            node = node.setdefault(part, {})  # type: ignore[assignment]
        node[name] = content

    def build(node: dict[str, object]) -> str:
        records = []
        for name in sorted(node):
            value = node[name]
            if isinstance(value, dict):
                records.append(f"040000 tree {build(value)}\t{name}".encode("utf-8", "surrogateescape"))
            else:
                oid = run_git(git_dir, "hash-object", "-w", "--no-filters", "--stdin",
                              input=value)  # type: ignore[arg-type]
                records.append(f"100644 blob {oid}\t{name}".encode("utf-8", "surrogateescape"))
        return run_git(git_dir, "mktree", "-z", "--missing", input=b"\0".join(records) + b"\0")

    return build(root)


def commit(git_dir: Path, files: Mapping[str, bytes], parents: Sequence[str], day: int,
           message: str = MISLEADING_MESSAGE) -> str:
    tree = write_tree(git_dir, files)
    args = ["commit-tree", tree]
    for parent in parents:
        args += ["-p", parent]
    date = f"2026-02-{day:02d}T00:00:00+0000" if day <= 28 else f"2026-03-{day - 28:02d}T00:00:00+0000"
    return run_git(git_dir, *args, "-m", message, date=date)


def set_branch(git_dir: Path, branch: str, commit_id: str) -> None:
    run_git(git_dir, "update-ref", f"refs/heads/{branch}", commit_id)


def delete_branch(git_dir: Path, branch: str) -> None:
    run_git(git_dir, "update-ref", "-d", f"refs/heads/{branch}")


def linear_history(directory: Path, snapshots: Sequence[Mapping[str, bytes]]) -> tuple[Path, list[str]]:
    """A bare repository whose ``main`` is ``snapshots`` committed in order."""
    git_dir = init_bare(directory / "upstream.git")
    commits: list[str] = []
    for index, files in enumerate(snapshots):
        commits.append(commit(git_dir, files, commits[-1:], index + 1))
    set_branch(git_dir, "main", commits[-1])
    return git_dir, commits


# -- the "classes" fixture: one step that exercises every change class -----------------------

PARAMS_OLD = b"""// mirrored defaults; last sync 2026-01-01T00:00:00Z
use feature_switches::param;

param!(ClickWeight, f64, "click_weight", 0.4);
param!(
    ReplyWeight,
    f64,
    "reply_weight",
    5.0
);
param!(EnableLegacy, bool, "enable_legacy", true);
param!(
    MaxResults,
    u32,
    "max_results",
    100
);
"""

PARAMS_NEW = b"""// mirrored defaults; last sync 2026-01-02T00:00:00Z
use feature_switches::param;

param!(ClickWeight, f64, "click_weight", 0.3);
param!(
    ReplyWeight,
    f64,
    "reply_weight",
    5.0
);
param!(
    MaxResults,
    u32,
    "max_results",
    100
);

param!(
    ExcludeSeen,
    bool,
    "exclude_seen",
    false
);
"""

PIPELINE_OLD = b"""pub struct Pipeline;

impl Pipeline {
    pub fn new() -> Self {
        let filters: Vec<Box<dyn Filter>> = vec![
            Box::new(AgeFilter::new()),
            Box::new(DedupFilter),
            Box::new(MutedKeywordFilter::new(3)),
        ];
        let sources: Vec<Box<dyn Source>> = vec![
            Box::new(InNetworkSource),
            Box::new(OutOfNetworkSource { limit: 10 }),
        ];
        Pipeline
    }
}
"""

PIPELINE_NEW = b"""pub struct Pipeline;

impl Pipeline {
    pub fn new() -> Self {
        let filters: Vec<Box<dyn Filter>> = vec![
            Box::new(DedupFilter),
            Box::new(AgeFilter::new()),
            Box::new(SpamFilter),
        ];
        let sources: Vec<Box<dyn Source>> = vec![
            Box::new(InNetworkSource),
            Box::new(OutOfNetworkSource { limit: 10 }),
        ];
        Pipeline
    }
}
"""

STRINGS_OLD = b"""pub fn trim(s: &str) -> &str {
    s.trim()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn trims() {
        assert_eq!(trim(" a "), "a");
    }
}
"""

STRINGS_NEW = b"""pub fn trim(s: &str) -> &str {
    s.trim_start()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn trims() {
        assert_eq!(trim(" a"), "a");
    }
}
"""

HANDLER_OLD = b"""pub fn handle(request: &Request) -> Response {
    let id = request.id();
    let user = lookup(id);
    let body = render(user);
    Response::ok(body)
}

fn lookup(id: u64) -> User {
    User::new(id)
}
"""

CLASSES_OLD: dict[str, bytes] = {
    "README.md": b"# Synthetic upstream\n\nFor tests.\n",
    "LICENSE": b"Synthetic license text, version 1.\n",
    "docs/guide.md": b"# Guide\n\nStep one.\n",
    "params/param.rs": PARAMS_OLD,
    "pipeline/pipeline.rs": PIPELINE_OLD,
    "scorers/weighted_scorer.rs": b"pub fn combine(a: f64, b: f64) -> f64 {\n    a * 0.5 + b\n}\n",
    "phoenix/models/features.py": b"def build(x):\n    return [x]\n",
    "util/strings.rs": STRINGS_OLD,
    "fmt/pretty.rs": b"// adds two numbers\npub fn add(a: i32, b: i32) -> i32 { a + b }\n",
    "tests/test_pipeline.py": b"def test_pipeline():\n    assert True\n",
    "vendor/lib/thing.py": b"VENDORED = 1\n",
    "config/app.yaml": b"server:\n  timeout_ms: 250\n  retries: 3\nname: synthetic\n",
    "util/constants.py": b"MAX_ITEMS = 10\nNAMES = compute()\n",
    "old/moved.rs": b"pub fn moved() -> u8 {\n    7\n}\n",
    "svc/handler.rs": HANDLER_OLD,
    "assets/blob.bin": b"\x00\x01binary\x00",
    "gone/removed.rs": b"pub fn removed() {}\n",
    "visibility/rules.rs": b"pub fn hide(flags: u8) -> bool {\n    flags > 2\n}\n",
    "svc/BUILD.bazel": b'rust_library(name = "svc", deps = [":a"])\n',
    "svc/wiring.rs": b"use crate::a::Alpha;\n\npub fn wire() -> u8 {\n    1\n}\n",
}

CLASSES_NEW: dict[str, bytes] = {
    "README.md": b"# Synthetic upstream\n\nFor tests, updated.\n",
    "LICENSE": b"Synthetic license text, version 2.\n",
    "docs/guide.md": b"# Guide\n\nStep one.\n\nStep two.\n",
    "params/param.rs": PARAMS_NEW,
    "pipeline/pipeline.rs": PIPELINE_NEW,
    "scorers/weighted_scorer.rs": b"pub fn combine(a: f64, b: f64) -> f64 {\n    a * 0.7 + b\n}\n",
    "phoenix/models/features.py": b"def build(x):\n    return [x, x]\n",
    "util/strings.rs": STRINGS_NEW,
    "fmt/pretty.rs": b"// Adds two numbers.\npub fn add(a: i32, b: i32) -> i32 {\n    a + b\n}\n",
    "tests/test_pipeline.py": b"def test_pipeline():\n    assert 1 == 1\n",
    "vendor/lib/thing.py": b"VENDORED = 2\n",
    "config/app.yaml": b"server:\n  timeout_ms: 300\n  retries: 3\nname: synthetic\n",
    "util/constants.py": b"MAX_ITEMS = 20\nNAMES = compute_all()\n",
    "new/moved.rs": b"pub fn moved() -> u8 {\n    7\n}\n",
    "svc/request_handler.rs": HANDLER_OLD.replace(b"User::new(id)", b"User::load(id)"),
    "assets/blob.bin": b"\x00\x02binary\x00",
    "added/fresh.rs": b"pub fn fresh() -> u8 {\n    1\n}\n",
    "visibility/rules.rs": b"pub fn hide(flags: u8) -> bool {\n    flags > 3\n}\n",
    "svc/BUILD.bazel": b'rust_library(name = "svc", deps = [":a", ":b"])\n',
    "svc/wiring.rs": b"use crate::a::{Alpha, Beta};\n// wiring\n\npub fn wire() -> u8 {\n    1\n}\n",
}


# -- the "range" fixture: several steps with reversions and a merge --------------------------

def range_params(click: str, legacy: str) -> bytes:
    return (
        "use feature_switches::param;\n\n"
        f"param!(ClickWeight, f64, \"click_weight\", {click});\n"
        f"param!(EnableLegacy, bool, \"enable_legacy\", {legacy});\n"
    ).encode()


def range_pipeline(filters: Sequence[str]) -> bytes:
    body = "".join(f"            Box::new({name}),\n" for name in filters)
    return (
        "impl Pipeline {\n    pub fn new() -> Self {\n"
        "        let filters: Vec<Box<dyn Filter>> = vec![\n" + body + "        ];\n"
        "        Pipeline\n    }\n}\n"
    ).encode()


def range_snapshots() -> list[dict[str, bytes]]:
    base = {"README.md": b"# Range fixture\n", "src/lib.rs": b"pub fn f() -> u8 {\n    1\n}\n"}
    return [
        {**base, "params/param.rs": range_params("0.4", "false"),
         "pipeline/pipeline.rs": range_pipeline(["AgeFilter", "DedupFilter"])},
        {**base, "params/param.rs": range_params("0.3", "true"),
         "pipeline/pipeline.rs": range_pipeline(["AgeFilter", "DedupFilter", "SpamFilter"])},
        {**base, "params/param.rs": range_params("0.3", "false"),
         "pipeline/pipeline.rs": range_pipeline(["AgeFilter", "DedupFilter"])},
        {**base, "params/param.rs": range_params("0.3", "false"),
         "pipeline/pipeline.rs": range_pipeline(["AgeFilter", "DedupFilter"]),
         "src/lib.rs": b"pub fn f() -> u8 {\n    2\n}\n"},
    ]


def range_history(directory: Path) -> tuple[Path, list[str], str]:
    """``c0 -> c1 -> c2 -> m -> c3`` where ``m`` merges a side commit ``s`` (parent c1)
    that adds ``docs/side.md``. Returns the git dir, ``[c0, c1, c2, m, c3]`` and ``s``."""
    snapshots = range_snapshots()
    git_dir = init_bare(directory / "upstream.git")
    c0 = commit(git_dir, snapshots[0], [], 1)
    c1 = commit(git_dir, snapshots[1], [c0], 2)
    c2 = commit(git_dir, snapshots[2], [c1], 3)
    side_files = {**snapshots[1], "docs/side.md": b"# Side\n"}
    side = commit(git_dir, side_files, [c1], 4)
    merged = {**snapshots[2], "docs/side.md": b"# Side\n"}
    merge = commit(git_dir, merged, [c2, side], 5)
    c3 = commit(git_dir, {**snapshots[3], "docs/side.md": b"# Side\n"}, [merge], 6)
    set_branch(git_dir, "main", c3)
    return git_dir, [c0, c1, c2, merge, c3], side
