# Contributing

TimelineXray is a public alpha (0.11.0). Contributor and agent rules (evidence
contract, hard boundaries, commit identity, no remote and no push without separate
approval) are in [AGENTS.md](AGENTS.md); read it first. The threat model is in
[SECURITY.md](SECURITY.md) and the known limits in [docs/limits.md](docs/limits.md). Everyone taking part follows the
[Code of Conduct](CODE_OF_CONDUCT.md). Issues use the forms in `.github/ISSUE_TEMPLATE`
(bug report, wrong or stale citation, feature request); blank issues are off, security
problems go through GitHub's private reporting and questions to Discussions.

## Set-up

Users install with `install.sh` (see [docs/install.md](docs/install.md)); contributors work
from a checkout as below, with no install step.

- Python 3.11, 3.12 or 3.13 (with SQLite FTS5 and the trigram tokenizer: `make pycheck`
  runs `scripts/check_sqlite.py`) and git 2.38 or newer (tested with 2.54 only; see
  `docs/limits.md`), on Linux or macOS. No runtime
  dependencies; nothing to install for development.
- A local clone of the upstream at the pinned commit, so that no test is skipped:

  ```sh
  python scripts/fetch_test_upstream.py ../x-algorithm-upstream
  export TXRAY_TEST_UPSTREAM="$PWD/../x-algorithm-upstream"
  ```

  (`../x-algorithm-upstream` next to the checkout is also found without the variable.)
  The script fetches `77d431aabf409ca1c1eed9bec7e2183f7c914e23` with its 39 commits of
  history over https; the tests themselves never use the network and pin from this clone
  through a `file://` URL.

## Checks

| Command | What it does |
| --- | --- |
| `make test` | the unittest suite through `scripts/run_tests.py`, in `JOBS` worker processes (`JOBS ?= auto`, which is `min(cpu count, 4)`; `make test JOBS=1` is serial) |
| `make lint` | byte-compile `src`, `tests` and `scripts`; `scripts/check_lint.py`; `scripts/check_workflows.py` |
| `make hygiene` | repository hygiene, privacy, licensing and module-boundary tests |
| `make test-strict` | the same runner and jobs; with `TXRAY_TEST_UPSTREAM` set, any skipped test (except an `optional:` one) fails the run |
| `make build-check` | `scripts/build_check.py`: sdist and wheel, clean-venv install, smoke test |
| `make ci` | exactly what the GitHub workflow runs |

`make ci` runs, in order: `lint`, `hygiene`, `test-strict`, `build-check`. It is the only
command the workflow `.github/workflows/ci.yml` runs after checking out, setting up Python
and cloning the upstream at the pinned commit (always: no cache, so the fetch script's integrity checks run every time); `scripts/check_workflows.py` fails when the
workflow, the Makefile and this list disagree. Always check the exit status of `make ci`
(`echo $?`), not the tail of its output.

`python scripts/run_tests.py --jobs N|auto` (or `TXRAY_TEST_JOBS`) runs the test modules in N
worker processes, longest first (`scripts/test_timing_hints.json` holds rough seconds per
module), and prints one combined summary with the serial runner's format, skip rule and exit
codes; the default is one process. The workers share the expensive fixtures through
`tests/templates.py`: the real-upstream stores and the synthetic repositories are built once
per run and copied into each test class (use a template only where a test does not look at how
the store was built; `UpstreamIndexTest` builds its own to test clean against incremental).

`scripts/check_lint.py` (stdlib `ast` only) is the lint gate of `make lint`: it reports
unused imports in `src`, `tests` and `scripts` and unused top-level private names. It
accepts names listed in `__all__`, names used in annotations (including `TYPE_CHECKING`
blocks), `from __future__` imports, every import of an `__init__.py` (the package's
re-exports) and lines marked `# noqa` or `# noqa: F401`. Fix a report by removing the
name, or mark a deliberate re-export with `# noqa: F401`.

**Network use of `make ci`.** TimelineXray itself never uses the network except its guarded
`git fetch` from the allowlisted upstream, and the tests block sockets. The `build-check`
step is the one exception in development: it creates an isolated virtual environment and
downloads the build requirement `setuptools>=77` from PyPI into it, builds the sdist and the
wheel, and installs the wheel into a second, fresh environment with `--no-index` (offline).

## The workflow

- Every third-party action is pinned to a full commit SHA with a comment naming its tag.
  To update one, resolve the new tag with `git ls-remote https://github.com/actions/<name>`
  and update the SHA and the comment together.
- `permissions: contents: read`, `persist-credentials: false`, a job timeout, concurrency
  cancellation, no secrets, and only `push`, `pull_request`, `workflow_dispatch` and
  `schedule` triggers (the weekly scheduled run, Mondays 05:17 UTC, catches drift of the
  runner image, Python, SQLite or git; `pull_request_target`, `workflow_run` and every other
  event stay refused). `scripts/check_workflows.py` enforces all of this.
- The matrix is Linux and macOS times the Python versions declared in `pyproject.toml`.

## Release gate

Before anything is pushed, the history of the branch to be published must pass
`python scripts/check_release_history.py --fsck` (exit 0): every commit has `+0000`
author and committer offsets and the project identity, no trailers, no local path, e-mail
address or secret-looking string in its message, and the repository holds no dangling
object (the script prints the pruning commands; run them in the repository you own, not in
a shared worktree). Extra patterns kept outside the repository go in `--patterns FILE`.
The sdist ships no `tests/` (`MANIFEST.in`; `scripts/build_check.py` checks it): the suite
reads the checkout (goldens, schemas, docs, git) and runs only from one.

Nothing is pushed without the owner's separate, explicit approval. After the owner has
approved pushing and the commit has been pushed, the release gate is:

```sh
python scripts/ci_status.py --repo OWNER/NAME --commit SHA
```

It queries the GitHub REST API for the commit's check runs, commit statuses and workflow
runs and exits 0 only when there is at least one and every one concluded `success` (of
several runs of one workflow on the commit only the latest counts: a re-run replaces a
run's conclusion, a new dispatch adds a run, and the older one is listed as `superseded`);
otherwise it prints a table and exits 1 (a failure), 3 (still pending), 4 (missing: no
checks, or a `--require NAME` check absent) or 5 (the API could not be queried). For a
private repository, provide a token in the environment only:
`GITHUB_TOKEN=... python scripts/ci_status.py ...`; the script never prints, logs or writes
it. Do not tag or announce a release until the gate passes.
