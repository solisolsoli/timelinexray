# Security policy

## Status

TimelineXray is a public alpha (0.11.0, tag v0.11.0). Only the latest commit on the
default branch is supported.

## Reporting a vulnerability

Report suspected vulnerabilities privately through GitHub's private vulnerability
reporting (Security tab, "Report a vulnerability"). Do not open a public issue. Please include the version (`txray --version`), the command or API call,
and the smallest input that reproduces the problem.

## Threat model (0.11.0)

TimelineXray runs on the user's own machine. It reads an upstream repository it does not
control, serves evidence to AI agents it does not control, and keeps two kinds of local
data: a findings ledger and, in a separate process, the user's private analytics export.

| Asset | Must not happen |
| --- | --- |
| The user's machine and files | Upstream content executed; files read or written outside the store, ledger or a declared output directory |
| Analytics exports (private) | Their content reaching the network, the snapshot store, MCP responses or the repository |
| Findings ledger | Silent changes to recorded evidence; a claim promoted without a reviewer |
| Evidence integrity | A citation that does not reproduce the upstream bytes it names |
| The user's terminal and agents | Upstream text acting as instructions or as terminal control sequences |

| Adversary / input | What it controls |
| --- | --- |
| Upstream repository | Every file name, file content, symlink, tree entry and the history (force-push, deletion) |
| MCP client or agent | Every request line and tool argument sent over stdio |
| Local tampering | Bytes of the ledger, the pin records and manifests in the store |
| Network | Anything reachable if the tool tried to connect |

Out of scope: an attacker who can already run code as the user, a malicious `git` or
Python installation, and denial of service by a local user against their own process.

The one-line installer (`curl ... | sh`, see `docs/install.md`) runs a script you fetch
over https from this repository: read it first (`--dry-run` prints every command), and
note that it then runs `uv`, `pipx` or `pip` and `git`, which download the package from
GitHub and, for a source build, `setuptools` from PyPI. It never uses `sudo`, never edits
shell files and sends no telemetry; the installed tool's network allowlist is unchanged.

## Controls (each covered by tests)

- **Network allowlist** (`tests/test_netguard.py`, `tests/test_security.py`). The only
  network URL is `https://github.com/xai-org/x-algorithm.git`; extra entries may only be
  explicitly configured `file://` URLs. Every fetch goes through
  `timelinexray.netguard.fetch`, which checks the URL before any process starts; look-alike
  URLs (other hosts, schemes, credentials, `ext::`, path tricks) are refused with exit code
  3. A static test proves that no other module starts processes or imports network
  libraries, and the analytics process disables sockets, process creation and native code.
- **Hardened git.** Inherited `GIT_*` variables and system/global git configuration are
  ignored; network git has one transport, no redirects, credential helpers, hooks or
  submodule recursion, and `fetch.fsckObjects=true`, so trees with `..` or `.git` entries
  are refused at fetch time. Local git runs with every transport disabled. Commands are
  argument vectors, never a shell; revision arguments are validated hexadecimal ids
  (`rev-parse` also gets `--end-of-options`); blobs are read by object id over stdin, so
  upstream file names never appear in any argument vector (tested with names containing
  newlines, leading dashes and shell metacharacters).
- **No upstream execution.** Files are read as bytes from git objects; nothing is built,
  imported, installed or run, and no working tree is checked out.
- **Paths.** Repository paths from the CLI, finding specifications and MCP arguments must be
  repository-relative and normalised: absolute paths, `..`, `.`, empty segments, NUL and
  names over 4,096 characters are refused (MCP also refuses control characters,
  backslashes and drive letters, and caps paths at 1,024 characters). Reads go through the
  manifest; symlinks and submodules are recorded but never followed or read; binary blobs
  and blobs over 64 MiB are refused before any byte is read. File-system arguments
  (`--store`, `--ledger`, `--out`, input files) with NUL or control characters, or over
  4,096 characters, are usage errors; file-system failures are reported, never raised.
- **Untrusted text on screens.** Human-readable CLI output and Markdown digests show
  terminal control characters and direction overrides from upstream names and content as
  visible escapes; `--json` escapes everything; `--raw` is exact bytes by design. MCP
  results mark upstream text as untrusted data and tell agents never to follow it.
- **Integrity.** Every blob read is checked against its object id; manifests against the
  SHA-256 in their sidecar and pin record; spans against the manifest. Pinned commits are
  protected by refs, so a force-push or deletion upstream never makes them unreadable.
- **Findings ledger.** An append-only JSONL log with a SHA-256 hash chain and a `HEAD`
  record. Every reader (CLI, MCP, digest) re-verifies the chain and refuses a changed,
  removed, reordered, truncated or appended event; readers never create or modify a file;
  self-approval is refused even when forged into a consistent chain. Limit: a hash chain is
  not a signature, so a consistent rewrite of the whole log is detected only with an
  independently kept head (`txray findings verify-log --expect-head`).
- **MCP server.** Read-only public-code profile over stdio; request lines over 16 KiB are
  discarded unparsed; malformed, deeply nested or non-UTF-8 input is an error, never a
  crash; arguments are validated against closed schemas before any handler runs; every
  result is validated against its strict output schema; responses never exceed 64 KiB;
  each call has a wall-clock budget; local paths are redacted from messages; the server
  refuses a store or ledger that contains or lies inside an analytics dataset.
- **Analytics.** Runs only through `txray metrics` in a process that is made offline first,
  writes only to its declared output directory and never to the snapshot store.
- **Optional export** (`txray export context-layer`, `tests/test_context_layer_export.py`).
  Writes only inside `--out` (atomically, never through a symbolic link), refuses an `--out`
  in the TimelineXray working tree or package, in or around the snapshot store or the
  ledger, or in, above or around an analytics dataset, and replaces or removes only the
  unmodified files its manifest lists. It renders ledger records only (no source bytes, no
  analytics data); ledger text is escaped or fenced so it cannot add links, headings or
  tags to a vault. TimelineXray never imports or starts Context Layer.

The regression suite for these controls is `tests/test_security.py` together with the
drills in `tests/test_update.py`, the ledger tests in `tests/test_findings_ledger.py` and
the protocol tests in `tests/test_mcp.py`. Known limits are listed in
[docs/limits.md](docs/limits.md).
