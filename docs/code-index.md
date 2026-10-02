# Code index (Milestone 2)

The code index answers three questions about a pinned commit: which lines contain some
text (`txray search`), which declarations exist (`txray symbols`), and which names are
called from which declarations (`txray symbols --calls`). Every answer names its commit,
path and 1-based line range, and carries the SHA-256 of the exact span bytes, so it can be
checked with `txray show`.

```
txray index   <commit> [--rebuild] [--coverage]
txray search  <commit> <query> [--path GLOB] [--limit N] [--literal]
txray symbols <commit> [--path GLOB] [--kind K] [--name N] [--limit N] [--calls]
```

All three take `--store DIR` and `--json` and use the Milestone 1 exit codes (0 ok,
1 failed, 2 invalid arguments). A commit must be pinned (`txray pin`) before it can be
indexed, and indexed before it can be searched.

## What is indexed

Input is the commit's verified manifest. Blob bytes come from the snapshot mirror (never a
working tree) and are checked against the manifest's size and SHA-256 before use.

| Manifest entry | Lexical status | Syntax status |
| --- | --- | --- |
| `parsed-candidate`, UTF-8 | `indexed` | `parsed`, `partial` (with reason) or `failed` (with reason) |
| `text`, UTF-8 | `indexed` | `not-applicable` |
| `parsed-candidate` or `text`, not UTF-8 | `skipped`, reason `not-utf8` | `skipped` |
| `excluded` | `skipped`, reason = the manifest reason | `skipped` |
| native file with no available backend | `indexed` | `unsupported` |

`txray index --coverage` lists every manifest path with these statuses, the reason, the
line counts and the syntax backend that produced its records; the summary counts them.

## Search

A query is split on whitespace into terms; `--literal` keeps the whole query (spaces
included) as one term. A hit is one line that contains every term as a case-insensitive
substring. ASCII case folding is exact; other scripts follow SQLite's trigram tokenizer.

- The index is SQLite FTS5 with the `trigram` tokenizer over non-blank lines. FTS5 only
  proposes candidate lines. Every term is passed to `MATCH` as a double-quoted FTS5 string
  with inner quotes doubled, so no query text is ever read as FTS5 syntax (`OR`, `NEAR`,
  `col:`, `^`, `*`, parentheses) or as SQL. Every candidate is then checked in Python, so
  a reported line always contains all terms.
- Terms shorter than three characters cannot use a trigram index. They are checked on the
  candidates of the longer terms; a query needs at least one term of three or more
  characters. Queries are limited to 512 characters and 16 terms.
- `--path` is an SQLite `GLOB` on the repository path: case-sensitive; `*` and `?` also
  match `/`.
- Hits are ordered by path bytes, then line. `total` counts every matching line; `--limit`
  (default 20, at most 1000) caps the hits returned.
- Each hit's span is cut from the blob with `timelinexray.span.read_span`, so its
  `span_sha256`, byte range and line numbers are exactly those `txray show` reports. The
  snippet is that line with surrounding whitespace trimmed, cut to 240 characters.

Search results are equal to a plain grep over the same files (tested against an
independent reader on fixtures and on the upstream commit).

Whenever a `search`, `symbols` or `show` output contains a digit, it ends with (for
`show`: its header carries) the line `note  numbers in the output above are public
defaults at commit <commit>, not production values`, and `--json` carries the same text in
a `note` member (`null` when no number is shown); `show --raw` writes only the span bytes.

## Symbols

| Kind | Rust | Scala | Java | Python |
| --- | --- | --- | --- | --- |
| `function` | free or nested `fn` | `def` outside a type body | - | `def` outside a class |
| `method` | `fn` in `impl`/`trait` | `def` in class/trait/object | methods | `def` in a class |
| `constructor` | - | `def this` | constructors | - |
| `class`, `trait`, `object`, `interface`, `enum`, `struct`, `union`, `record`, `annotation` | as declared | `class`/`case class`, `trait`, `object`/`case object`/`package object` | `class`, `interface`, `enum`, `record`, `@interface` | `class` |
| `impl` | `impl` block; name = implementing type | - | - | - |
| `const`, `static` | `const`, `static` | - | `static final` fields, interface fields | module/class `UPPER_CASE = ...` |
| `field` | - | `val`/`var` members | other fields | - |
| `param` | `param!(Name, ...)` | - | - | - |
| `type`, `macro`, `module`, `import` | `type`, `macro_rules!`, `mod`, `use` / `extern crate` | `type`, -, `package`, `import` | -, -, `package`, `import` | -, -, -, `import`/`from` |

Each symbol has `name`, `container` (enclosing declarations; `qualname` joins them with
`::` for Rust and `.` otherwise), `start_line` (including attributes, annotations or
decorators directly above), `name_line`, `end_line` (closing brace, terminator or last body
line), `span_sha256` of lines `start_line-end_line`, `signature` (the header without
comments, whitespace collapsed, at most 240 characters; constants and `param!` include
their value) and `modifiers` (visibility, keywords, annotation or decorator names).
`--name` matches `name` or `qualname` exactly, or as a GLOB when it contains `*`, `?` or
`[`. Import names are the statement text after its keyword.

## Parameters

```
txray param         <name> [--commit C]            declarations and public default at a commit
txray param-history <name> [--base C] [--head C]   value at every pinned commit of one line
```

`txray param` lists every `const`, `static`, `field` or `param!` declaration with the given
name or qualified name at an indexed commit (default: the newest pin), found through the
index and read from the blob with the same value extraction `txray diff` uses
(`timelinexray.params`): the literal public default, the declared type and flag string of a
`param!`, the exact span with its SHA-256, and up to ten lines mentioning the name
elsewhere (lexical, unresolved). `txray param-history` follows the declarations over the
pinned commits on the first-parent chain from `--base` to `--head` (default: the oldest to
the newest pin of the same mirror), from objects already in the mirror: the value at each
pinned commit with its committer time, every change (`declared`, `value-changed`,
`removed`) with the citation on both sides, reversions, and gaps where commits between two
pins are not pinned. A pinned commit that is not indexed is checked only at the paths where
indexed commits declare the name, and the output says so. Names are exact (no GLOB; use
`txray symbols --name`); config-file keys are not reachable by name. The MCP tools
`get_param` and `param_history` give the same answers ([mcp.md](mcp.md)).

## Call candidates

A call candidate is a name directly followed by an argument list inside the body of a
function, method, constructor, or the initializer of a constant, static or field. It is
reported as relation `CANDIDATE_CALL`, resolution `unresolved`, with the caller's qualified
name, the callee as written, a `qualifier` when the receiver or path is a plain chain
(`self.weights`, `Box`), a form (`call`, `method`, `path`, `macro`, `new`) and the line.

Candidates are syntax, not resolution: nothing checks which declaration a name refers to.
Constructors and patterns that look like calls (`Some(x)`) are included; keywords,
declarations, annotations, Rust attributes, Scala `case X(...)` patterns and Rust
`dyn Fn(...)` types are not. Calls without parentheses (Scala infix or parameterless calls)
and calls inside Scala string interpolations are not seen.

## Storage, scoping and incremental builds

The index is one SQLite database per store, `<store>/index/index.sqlite3`
(`PRAGMA user_version` = 1).

- Commit-scoped tables carry the commit id in every row: `generations` (one per indexed
  commit: manifest SHA-256, syntax backend per language, totals) and `files` (one per
  manifest path: blob id, statuses, reasons, the parse it uses).
- Content-addressed tables are keyed by blob id: `blobs`, `blob_lines` (FTS5; rowid =
  `blob_key << 24 | line`), `parses` (one per `(blob_oid, language, backend,
  backend_version)`, the parse-cache key of P7 section 2.3), `symbols` and `calls`.
- Every query starts from one commit's `files` rows and every result carries that commit,
  so content shared by several commits is never reported for a commit that lacks it.
  Keeping lines per blob instead of per commit is what makes an incremental build touch
  only new blobs; at 77d431a the lexical table alone is most of the ~93 MB database.
- `txray index` reads from git and parses only blobs that the index does not have yet
  (for the selected backend and version). Re-running it for an indexed commit with the
  same manifest and backends does nothing. `--rebuild` re-derives every blob of the commit
  in place, keeping internal keys, so other commits sharing those blobs stay consistent.
- A build is one SQLite transaction: an interrupted build leaves no partial generation.
- The database runs in WAL journal mode (set at creation; a file made by an earlier
  version is converted the next time `txray index` opens it). Readers (`txray search`,
  `txray symbols`, the MCP server, the verifier) see the last committed generation and
  never wait for a running build; their busy timeout is 5 s, which only matters for a
  not-yet-converted file.
- Integer keys are internal and never appear in results. Tests compare an incremental and
  a clean build of the same commit both through every query and as a full logical dump.

## Syntax backends

`timelinexray.syntax.Backend` is the interface: `name`, `version`, `languages`, `probe()`
(`None` when usable, else a reason) and `extract(text, language)`. A `Registry` holds
backends in priority order; the first available one for a language is used, and the
backend and version of every parse are recorded per file and reported per symbol.

- `lexical` (always available, standard library only) masks comments and literals, finds
  declarations with per-language patterns anchored at statement starts, and computes
  extents from delimiter matching (Rust, Java, Scala) or indentation (Python). Problems it
  cannot recover from exactly (unbalanced delimiters, unterminated literals) mark the file
  `partial` with a reason rather than failing it. Any exception from a backend marks the
  file `failed` and the build continues.
- `tree-sitter` is a hook only. No tree-sitter runtime or grammar could be installed
  offline for this milestone, so none is a dependency; the registered hook always reports
  why it is unavailable and is never selected. `timelinexray/syntax/treesitter.py` states
  the contract a future `[syntax]` extra must meet: pinned runtime and grammar versions, a
  version string that includes the grammar pins (so the parse cache never mixes results),
  the same record vocabulary, `partial` for trees with errors, and no execution of
  upstream code.

## Measured at 77d431a

Measured on the development machine (macOS, Python 3.12, SQLite 3.45) while writing this
milestone; reported, not promised. The upstream test prints the same measurements on the
machine that runs it.

| | |
| --- | --- |
| Paths | 2147: 2140 indexed lexically (380,116 non-blank lines), 7 skipped (1 binary, 5 generated, 1 symlink) |
| Syntax | 1836 native files parsed by `lexical/1`, 0 partial, 0 failed; 304 text files not applicable |
| Symbols | 50,387: Rust 17,029, Scala 17,179, Python 9,926, Java 6,253 |
| Call candidates | 151,726 (all unresolved) |
| Clean build, empty index | 7.0-8.2 s |
| Incremental build after a707cc2 (65 changed blobs) | 0.9-1.0 s |
| Database size | 92.7 MB for 77d431a; 102.3 MB with a707cc2 added |
| Search | about 0.03 s for an identifier such as `PhoenixScorer`; about 0.1 s for a very common trigram such as `the` |

## Known limits

- Lexical extraction is heuristic. Unusual formatting can shift an end line; Scala
  expression bodies end by indentation. Rust enum variants, struct fields and Java enum
  constants are not reported. Methods of anonymous classes are reported as functions of
  the enclosing declaration.
- Names are never resolved (see Call candidates). Macros are not expanded.
- Non-UTF-8 text files are not searchable (they are listed as `skipped`, `not-utf8`).
