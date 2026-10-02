# Installing TimelineXray

TimelineXray is a pure standard-library Python package with no runtime dependencies. It
needs, on Linux or macOS (Windows is not supported, see [limits](limits.md)):

- Python 3.11 or newer whose SQLite has FTS5 with the trigram tokenizer (SQLite 3.34+);
- git 2.38 or newer on `PATH`.

## One command

```sh
curl -fsSL https://raw.githubusercontent.com/solisolsoli/timelinexray/main/install.sh | sh
```

The script checks your machine first (it prints what it found and stops with an
actionable message and a distinct exit code if something is missing), installs
`txray` with `uv`, `pipx` or a private virtual environment, runs `txray --version` to
verify it, and prints the next step. It never edits your shell files, never uses `sudo` and
sends no telemetry; the only network access is what `uv`, `pipx`, `pip` and `git` need to
fetch the package from this repository.

### Inspect first

```sh
curl -fsSLO https://raw.githubusercontent.com/solisolsoli/timelinexray/main/install.sh
less install.sh
sh install.sh --dry-run     # prints every command, changes nothing
sh install.sh
```

The whole script is wrapped in functions and ends with a single `main "$@"` call, so a
download that is cut short does nothing.

## Options

| Option | Environment | Meaning |
| --- | --- | --- |
| `--method auto\|uv\|pipx\|venv` | `TXRAY_INSTALL_METHOD` | `auto` (default) uses `uv` if it is on `PATH`, else `pipx`, else a private venv |
| `--ref REF` | `TXRAY_INSTALL_REF` | branch, tag or commit to install (default `main`) |
| `--source PATH` | `TXRAY_INSTALL_SOURCE` | install a local checkout directory or a wheel file instead of GitHub; a wheel is installed without an index (offline) |
| `--uninstall` | | remove what the installer installed |
| `--dry-run` | | print every command and change nothing |
| `--help` | | show the options and exit codes |
| | `TXRAY_PYTHON` | the interpreter to use, one path (spaces are fine) (default: first of `python3`, `python3.13`, `python3.12`, `python3.11` that is new enough and has FTS5) |
| | `TXRAY_HOME` | venv method: data directory (default `${XDG_DATA_HOME:-$HOME/.local/share}/timelinexray`; a relative path is taken relative to the current directory and made absolute) |
| | `TXRAY_BIN_DIR` | venv method: where the `txray` link goes (default `$HOME/.local/bin`; made absolute like `TXRAY_HOME`; `HOME` is needed only when one of the two is unset) |

Flags override the environment. Pass options through the pipe with `sh -s --`:

```sh
curl -fsSL https://raw.githubusercontent.com/solisolsoli/timelinexray/main/install.sh | sh -s -- --method pipx --ref main
```

What each method runs (`<python>` is the interpreter that passed the checks):

- `uv`: `uv tool install --force --python <python> <spec>`
- `pipx`: `pipx install --force --python <python> <spec>`
- `venv`: creates `$TXRAY_HOME/venv`, runs `pip install <spec>` in it and links
  `txray` into `$TXRAY_BIN_DIR`

`<spec>` is `git+https://github.com/solisolsoli/timelinexray@<ref>`, or the `--source` path.
Installing from a directory builds the package, which downloads the build requirement
`setuptools>=77` from PyPI; a wheel needs no download.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | success |
| 2 | usage error (unknown option, bad value, bad `--source`) |
| 10 | unsupported operating system (Windows, BSD, ...) |
| 11 | Python missing or older than 3.11 |
| 12 | no Python with SQLite FTS5 and the trigram tokenizer |
| 13 | git missing or older than 2.38 |
| 14 | the method you asked for (`uv`, `pipx`) is not on `PATH` |
| 15 | the installation command failed, or `--uninstall` could not remove a `uv` or `pipx` install (the failing tool is named; the other methods are still tried first) |
| 16 | the installed `txray` is missing or does not run |

## Alternatives without the script

```sh
uv tool install git+https://github.com/solisolsoli/timelinexray
pipx install git+https://github.com/solisolsoli/timelinexray
pip install "git+https://github.com/solisolsoli/timelinexray"   # into an environment you manage
```

Pin a release or commit by appending `@REF`. Check the result with `txray --version`.

## Next steps

If your `txray` has the `txray setup` command, run it. Otherwise pin and index the upstream
commit the documentation uses:

```sh
txray pin 77d431aabf409ca1c1eed9bec7e2183f7c914e23 && txray index 77d431a
```

## Uninstall

```sh
sh install.sh --uninstall                       # uv, pipx and the venv, whichever is present
sh install.sh --uninstall --method venv         # only the private venv and its link
uv tool uninstall timelinexray                  # by hand
pipx uninstall timelinexray
```

Uninstalling never touches your data: snapshot stores, indexes and the findings ledger are
yours and stay where they are.

## Troubleshooting

**"no Python 3.11+ with SQLite FTS5 and the trigram tokenizer" (exit 12).** The code index
uses FTS5 with `tokenize = 'trigram'`, which needs SQLite 3.34 or newer compiled with
FTS5. A few Python builds (some distribution and older system Pythons) lack it. Check a
Python with the same probe the installer uses:

```sh
python3 scripts/check_sqlite.py      # from a checkout
python3 -c "import sqlite3; sqlite3.connect(':memory:').execute(\"create virtual table t using fts5(x, tokenize='trigram')\")"
```

Get a Python that has it and point the installer at it:

```sh
uv python install 3.12                   # uv's managed builds include FTS5
brew install python                      # macOS, Homebrew
TXRAY_PYTHON="$(uv python find 3.12)" sh install.sh
```

The installers from python.org and the official Docker images also work.

**"Python 3.11 or newer is required" (exit 11).** Install a newer Python (`brew install
python`, `apt install python3`, `uv python install 3.12`) or set `TXRAY_PYTHON`.
On Debian and Ubuntu the venv method may also need `apt install python3-venv`.

**`txray: command not found` after a successful install.** The bin directory is not on
`PATH`. The installer prints the exact line to add, for example:

```sh
export PATH="$HOME/.local/bin:$PATH"
```

Put it in your shell's startup file yourself (the installer never edits those files), then
open a new terminal. `uv tool update-shell` and `pipx ensurepath` do the same for their
directories.

**Behind a proxy.** `curl`, `git`, `pip`, `uv` and `pipx` all read the standard variables:

```sh
export HTTPS_PROXY=http://proxy.example:3128
export NO_PROXY=localhost,127.0.0.1
curl -fsSL https://raw.githubusercontent.com/solisolsoli/timelinexray/main/install.sh | sh
```

For a private index mirror set `PIP_INDEX_URL` (or `UV_INDEX_URL`). With no access to
GitHub at all, copy a checkout or a wheel to the machine and use
`sh install.sh --source /path/to/checkout-or-wheel`.

**Corporate TLS inspection.** If pip or git reject the proxy's certificate, point them at
your CA bundle (`SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `GIT_SSL_CAINFO`) instead of
disabling verification.
