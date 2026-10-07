"""Change classes and the path rules of the change classifier.

The classifier is deterministic and mechanical. Its rules are heuristics over paths,
manifest classifications, Milestone 2 symbols and masked source text; a class names the
*evidence the rule saw*, never a reviewed interpretation. Commit messages are never read.

Bump :data:`CLASSIFIER_VERSION` whenever the output for unchanged inputs may change.
"""

from __future__ import annotations

import posixpath
import re

from ..snapshot.classify import license_kind

CLASSIFIER_VERSION = 4

PARAMETER_DEFAULT = "parameter-default"
REGISTRATION = "registration"
SCORING_LOGIC = "scoring-logic"
MODEL_CONFIG = "model-config"
LICENSE = "license"
DOCS_ONLY = "docs-only"
TEST_ONLY = "test-only"
BUILD_DEPENDENCY = "build-dependency"
ACCESS_MODIFIER = "access-modifier"
DATA_TYPE = "data-type"
OBSERVABILITY = "observability"
VISIBILITY_RULE = "visibility-rule"
COSMETIC = "cosmetic"
GENERATED_VENDORED = "generated-vendored"
UNKNOWN = "unknown"

#: Every class, in reporting order.
CLASSES = (
    PARAMETER_DEFAULT,
    REGISTRATION,
    SCORING_LOGIC,
    VISIBILITY_RULE,
    MODEL_CONFIG,
    LICENSE,
    DOCS_ONLY,
    TEST_ONLY,
    BUILD_DEPENDENCY,
    DATA_TYPE,
    OBSERVABILITY,
    ACCESS_MODIFIER,
    COSMETIC,
    GENERATED_VENDORED,
    UNKNOWN,
)

CLASS_TITLES = {
    PARAMETER_DEFAULT: "Parameter default change",
    REGISTRATION: "Filter/pipeline registration change",
    SCORING_LOGIC: "Weight/scoring logic change",
    VISIBILITY_RULE: "Visibility rule definition change",
    MODEL_CONFIG: "Model/config change",
    LICENSE: "License/notice change",
    DOCS_ONLY: "Documentation-only change",
    TEST_ONLY: "Test-only change",
    BUILD_DEPENDENCY: "Build/dependency/import change",
    DATA_TYPE: "Type definition change",
    OBSERVABILITY: "Logging/metrics-only change",
    ACCESS_MODIFIER: "Access-modifier-only change",
    COSMETIC: "Formatting-only/cosmetic change",
    GENERATED_VENDORED: "Generated or vendored file",
    UNKNOWN: "Unknown / unresolved change",
}

#: What each class rests on (shown in digests), and what it must not be read as.
CLASS_EVIDENCE = {
    PARAMETER_DEFAULT: "the value of a param!(...) declaration, a const/static, a literal "
    "field or a config key differs between the two cited declarations",
    REGISTRATION: "entries of a list that registers components (filters, sources, hydrators, "
    "scorers, side effects, rules, ...) were added, removed or reordered",
    SCORING_LOGIC: "changed code whose path, or else the production symbol enclosing a "
    "changed line, has a word naming scoring, weights or ranking (score, weight, rank, boost, "
    "decay, blend, ...; not PageRank or RankAll, not a metrics, stats or debug name, not "
    "kernel or training code, not a hunk that only logs or only adds type fields): a name "
    "heuristic, not a semantic analysis; in a hand check 9 of 19 recent and 2 of 13 "
    "full-history items were scoring code (docs/updates.md)",
    VISIBILITY_RULE: "every changed code line lies inside a Rust const or function whose declared "
    "or return type is built only from the visibility rule types Condition, Predicate, Clause "
    "and RuleClause (a declared-type rule, Milestone 2 symbols), and no name rule matched",
    MODEL_CONFIG: "changed code or configuration in a model, feature, config, schema, proto, "
    "thrift, inference, train, training or optimizers location, or GPU kernel code (cuda, "
    "cutedsl, pallas, triton, kernels directories; .cu/.cuh files): a path heuristic",
    LICENSE: "a license or notice file, or license header lines, changed",
    DOCS_ONLY: "only documentation files changed",
    TEST_ONLY: "only test files or test regions changed (a path and file-name heuristic, "
    "plus test symbols such as #[cfg(test)] modules and items; blank and comment lines may "
    "accompany them)",
    BUILD_DEPENDENCY: "a build or dependency manifest changed (a file-name heuristic: BUILD, "
    "*.bazel, Cargo.toml, build.rs, requirements*.txt, Makefile, ...), or every changed code "
    "line is an import, use, extern crate, bodyless mod or package declaration (Milestone 2 "
    "symbols; an empty __init__.py is a package marker, py.typed a build file)",
    DATA_TYPE: "every changed code line lies inside a Rust struct, enum or union definition "
    "(fields, variants, their attributes; Milestone 2 symbols) and assigns no value (no '=': "
    "no discriminant, no attribute default), and no name rule matched other than a scoring "
    "path (a type whose own name is a scoring name stays scoring-logic)",
    OBSERVABILITY: "every changed code line belongs to a statement that only logs, traces or "
    "records a metric (Rust log/tracing/metrics macros and Prometheus counters, Python logger "
    "and metrics calls, Java/Scala log and stats calls; masked code), and no model/config "
    "path rule matched (it wins over the scoring-name rule)",
    ACCESS_MODIFIER: "the comment- and whitespace-insensitive tokens of both sides are equal once "
    "access modifiers are removed (Rust pub, pub(crate), pub(super), pub(in path); Java public, "
    "private, protected; Scala private/protected[scope])",
    COSMETIC: "only comments, whitespace or line endings changed (token sequences are "
    "equal), or a file moved without content change",
    GENERATED_VENDORED: "the manifest classifies the file as generated or vendored",
    UNKNOWN: "a change the rules cannot classify reliably; it needs review",
}

#: What each class must not be read as (shown in digests next to the evidence).
CLASS_CAVEATS = {
    PARAMETER_DEFAULT: "a production value: every value is a public default at its commit",
    REGISTRATION: "that a registered component is active for any request",
    SCORING_LOGIC: "that the change alters any ranking outcome",
    VISIBILITY_RULE: "that any post's visibility changed: the definition may be refactored, and "
    "whether a rule runs depends on its registration and safety level",
    MODEL_CONFIG: "that a model artifact was deployed",
    LICENSE: "a determination of the applicable license",
    DOCS_ONLY: "a change of behaviour",
    TEST_ONLY: "a change of production code",
    BUILD_DEPENDENCY: "that behaviour is unchanged",
    DATA_TYPE: "that behaviour is unchanged: a new field or variant changes what code can "
    "store, match and serialize",
    OBSERVABILITY: "that behaviour is unchanged: a logged or counted expression can have "
    "side effects, and a metric can drive alerts or experiments",
    ACCESS_MODIFIER: "that behaviour is unchanged: a newly visible item can be used, and a "
    "hidden one no longer can, from other modules",
    COSMETIC: "semantic equivalence where parser coverage is incomplete",
    GENERATED_VENDORED: "reviewed upstream code",
    UNKNOWN: "anything: it needs review",
}

NATIVE_LANGUAGES = frozenset({"java", "python", "rust", "scala"})

_DOC_EXTENSIONS = frozenset({".md", ".markdown", ".rst", ".adoc", ".txt"})
_DOC_DIRS = frozenset({"doc", "docs", "documentation"})
_DOC_NAMES = re.compile(r"(?i)^(readme|changelog|changes|contributing|authors|code_of_conduct|"
                        r"history|security)(\.[a-z0-9]+)?$")
_NOT_DOC_NAMES = re.compile(r"(?i)^(requirements.*|constraints.*|cmakelists)\.txt$")

_TEST_DIRS = frozenset(
    {
        "__tests__", "benches", "fixture", "fixtures", "golden", "golden_corpus", "goldens",
        "integration-tests", "integration_test", "integration_tests", "integrationtest",
        "loadtest", "spec", "specs", "test", "test-data", "test_data", "test_utils",
        "testdata", "testing", "tests", "testutil", "testutils",
    }
)
_TEST_NAMES = re.compile(
    r"^(test_[^/]*\.py|conftest\.py|[^/]*_tests?\.(py|rs|go|scala|java)|"
    r"[^/]*(Test|Tests|Spec|Suite|IT)\.(java|scala)|"
    r"(fixtures?|testutils?|test_utils?|test_support|test_helpers?)\.(rs|py|scala|java)|"
    r"[^/]*_fixtures?\.(rs|py|scala|java))$"
)

#: Words (lower case, after splitting a name at non-alphanumerics and camelCase humps) that
#: name scoring, weights or ranking. Whole words only: ``rankall`` or ``unscored`` are not.
_SCORING_WORD = re.compile(
    r"(?:re)?(?:scor(?:e|es|ed|er|ers|ing)|rank(?:s|ed|er|ers|ing|ings)?)"
    r"|weight(?:s|ed|ing)?|boost(?:s|ed|ing)?|penalt(?:y|ies)|decay(?:s|ed|ing)?"
    r"|diversit(?:y|ies)|blend(?:s|ed|er|ers|ing)?|multipliers?"
    r"|calibrat(?:e|es|ed|ing|ion|ions|or|ors)"
)
#: Word sequences that name a system, not scoring: PageRank (a user reputation graph score
#: used by spam and bot rules) and Phoenix RankAll (an event-to-index pipeline). A name that
#: contains one of them is not a scoring name, whatever else it contains (hand check,
#: classifier version 3, docs/updates.md).
_NOT_SCORING_PHRASES = (("page", "rank"), ("pagerank",), ("rank", "all"), ("rankall",))
#: Words that name telemetry or debugging. A path whose file name, or a symbol whose last
#: component, has one of them is not a scoring name for the classifier (``SCORED_METRIC``,
#: ``get_debug_scored_posts``, ``scored_stats_side_effect.rs``; classifier version 4).
_TELEMETRY_WORDS = frozenset({"metric", "metrics", "stat", "stats", "statistics", "debug"})
_NAME_CHUNK = re.compile(r"[A-Za-z0-9]+")
_NAME_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")
_MODEL_CONFIG_DIRS = re.compile(
    r"(?i)^(models?|features?|configs?|conf|settings|schemas?|protos?|thrift|embeddings?|"
    r"checkpoints?|inference|train|training)$"
)
#: Model-side locations: GPU kernel code of a model (CUDA, CuTe DSL, Pallas, Triton) and
#: offline training code (train, training, optimizers). A changed line there is model code,
#: whatever its name says (``ranker_attention.py`` is an attention kernel of the ranker model),
#: so this path rule is read before the scoring-name rules (classifier version 4).
_MODEL_SIDE_DIRS = re.compile(
    r"(?i)^(cuda|cutedsl|pallas|triton|kernels|train|training|optimizers)$"
)
_MODEL_SIDE_EXTENSIONS = frozenset({".cu", ".cuh"})
_MODEL_CONFIG_NAMES = re.compile(r"(?i)(config|feature|model|schema|embedding)")
_CONFIG_EXTENSIONS = frozenset(
    {".cfg", ".conf", ".ini", ".json", ".properties", ".proto", ".thrift", ".toml", ".yaml", ".yml"}
)
#: Config-like files whose keys are build or dependency metadata, not runtime defaults.
_BUILD_MANIFESTS = frozenset(
    {
        "cargo.toml", "package.json", "pyproject.toml", "rust-toolchain.toml", "tsconfig.json",
        "pipfile", "setup.cfg", "tox.ini", ".pre-commit-config.yaml", "rustfmt.toml",
        "clippy.toml", "deny.toml", "buf.yaml", "buf.gen.yaml",
    }
)
#: Build-system and dependency files by name (lower case): the ``build-dependency`` path rule.
_BUILD_NAMES = _BUILD_MANIFESTS | frozenset(
    {
        "build", "build.bazel", "workspace", "workspace.bazel", "module.bazel", ".bazelrc",
        ".bazelversion", "build.rs", "cargo.lock", "rust-toolchain", ".rustfmt.toml",
        "setup.py", "manifest.in", "pipfile.lock", "makefile", "gnumakefile", "cmakelists.txt",
        "dockerfile", "containerfile", "pom.xml", "build.gradle", "build.gradle.kts",
        "settings.gradle", "settings.gradle.kts", "build.sbt", "go.mod", "go.sum",
        "package-lock.json", "yarn.lock", "pnpm-lock.yaml", ".python-version", ".nvmrc",
        "py.typed",
    }
)
_BUILD_EXTENSIONS = frozenset({".bazel", ".bzl", ".cmake", ".gradle", ".mk"})
_BUILD_NAME_PATTERN = re.compile(r"(?i)^(requirements|constraints)([-_.][a-z0-9_.-]*)?\.(txt|in)$")
#: Config formats whose ``key = value`` / ``key: value`` lines are read as parameters.
KEY_VALUE_EXTENSIONS = frozenset({".cfg", ".conf", ".ini", ".json", ".properties", ".toml",
                                  ".yaml", ".yml"})


def _segments(path: str) -> list[str]:
    return path.split("/")


def is_license_path(path: str) -> bool:
    return license_kind(path) is not None


def is_docs_path(path: str) -> bool:
    name = posixpath.basename(path)
    if is_license_path(path) or _NOT_DOC_NAMES.match(name):
        return False
    if _DOC_NAMES.match(name):
        return True
    if any(part.lower() in _DOC_DIRS for part in _segments(path)[:-1]):
        return True
    return posixpath.splitext(name)[1].lower() in _DOC_EXTENSIONS


def is_test_path(path: str) -> bool:
    parts = _segments(path)
    if any(part in _TEST_DIRS for part in parts[:-1]):
        return True
    return bool(_TEST_NAMES.match(parts[-1]))


def is_build_path(path: str) -> bool:
    """Build-system or dependency manifests (``BUILD``, ``*.bazel``, ``Cargo.toml``, ``build.rs``,
    ``requirements*.txt``, ``Makefile``, ``Dockerfile``, ...), by file name only."""
    name = posixpath.basename(path)
    lowered = name.lower()
    if lowered in _BUILD_NAMES or _BUILD_NAME_PATTERN.match(name):
        return True
    return posixpath.splitext(lowered)[1] in _BUILD_EXTENSIONS


def name_words(text: str) -> list[str]:
    """The lower-case words of a path or symbol name: split at every non-alphanumeric
    character and at camelCase humps (``PhoenixRankAll`` -> phoenix, rank, all)."""
    words: list[str] = []
    for chunk in _NAME_CHUNK.findall(text):
        words.extend(word.lower() for word in _NAME_WORD.findall(chunk))
    return words


def is_scoring_name(text: str | None) -> bool:
    """Whether a path or symbol name names scoring, weights or ranking: one of its words is a
    scoring word and it names none of the excluded systems (PageRank, Phoenix RankAll)."""
    if not text:
        return False
    words = name_words(text)
    for phrase in _NOT_SCORING_PHRASES:
        size = len(phrase)
        if any(tuple(words[i:i + size]) == phrase for i in range(len(words) - size + 1)):
            return False
    return any(_SCORING_WORD.fullmatch(word) for word in words)


def is_telemetry_name(text: str | None, *, path: bool) -> bool:
    """Whether the file name of a path (``path=True``, extension removed) or the last
    component of a symbol name (after the final ``::`` or ``.``) has a telemetry or debugging
    word (:data:`_TELEMETRY_WORDS`)."""
    if not text:
        return False
    if path:
        tail = posixpath.splitext(posixpath.basename(text))[0]
    else:
        tail = re.split(r"::|\.", text)[-1]
    return bool(_TELEMETRY_WORDS.intersection(name_words(tail)))


def is_scoring_path(path: str | None) -> bool:
    """The classifier's scoring path rule: :func:`is_scoring_name` and no telemetry word in
    the file name (:func:`is_telemetry_name`)."""
    return is_scoring_name(path) and not is_telemetry_name(path, path=True)


def is_scoring_symbol(name: str | None) -> bool:
    """The classifier's scoring symbol rule: :func:`is_scoring_name` and no telemetry word in
    the last name component (:func:`is_telemetry_name`)."""
    return is_scoring_name(name) and not is_telemetry_name(name, path=False)


def is_model_side_path(path: str) -> bool:
    """GPU kernel or offline training code of a model, by location: a directory named
    ``cuda``, ``cutedsl``, ``pallas``, ``triton``, ``kernels``, ``train``, ``training`` or
    ``optimizers``, or a CUDA source file (``.cu``, ``.cuh``)."""
    parts = _segments(path)
    if any(_MODEL_SIDE_DIRS.match(part) for part in parts[:-1]):
        return True
    return posixpath.splitext(parts[-1])[1].lower() in _MODEL_SIDE_EXTENSIONS


def is_model_config_path(path: str) -> bool:
    if is_model_side_path(path):
        return True
    parts = _segments(path)
    if any(_MODEL_CONFIG_DIRS.match(part) for part in parts[:-1]):
        return True
    name = parts[-1]
    if posixpath.splitext(name)[1].lower() in _CONFIG_EXTENSIONS:
        return True
    return bool(_MODEL_CONFIG_NAMES.search(posixpath.splitext(name)[0]))


def is_key_value_config(path: str) -> bool:
    """Config files whose ``key: value`` lines are compared as parameter defaults."""
    name = posixpath.basename(path)
    if name.lower() in _BUILD_MANIFESTS:
        return False
    return posixpath.splitext(name)[1].lower() in KEY_VALUE_EXTENSIONS


def path_class(path: str, reason: str | None) -> str | None:
    """Class decided by the path and manifest alone, or ``None`` when content decides.

    Order: license/notice file, generated or vendored (manifest reason), documentation,
    tests, build or dependency manifest.
    """
    if is_license_path(path):
        return LICENSE
    if reason in ("generated", "vendored"):
        return GENERATED_VENDORED
    if is_docs_path(path):
        return DOCS_ONLY
    if is_test_path(path):
        return TEST_ONLY
    if is_build_path(path):
        return BUILD_DEPENDENCY
    return None


def logic_match(paths: tuple[str, ...],
                symbol_names: tuple[str, ...]) -> tuple[str, str | None, str | None]:
    """Class of a changed region that is neither a parameter, registration nor cosmetic,
    with the rule that decided it: ``(class, "path" | "symbol" | None, matched name)``.

    Order: a model-side path (:func:`is_model_side_path`: kernel or training code), a
    scoring-related path (:func:`is_scoring_path`), a model/config path, a scoring-related
    enclosing symbol name (:func:`is_scoring_symbol`), else unknown (the caller passes only
    symbols that enclose a changed production line). (A filtering/visibility name rule was tried for classifier version 2
    and rejected: in a hand check most of its items were caches, telemetry and tooling, so
    such changes stay ``unknown``; see ``docs/updates.md``.)
    """
    # Telemetry names are read only when every enclosing symbol was read: a longer list ends
    # with "... N more", and the unread symbols may name scoring code. When every enclosing
    # symbol has a telemetry name (SCORE_METRICS_SAMPLE_RATE), a scoring path does not decide.
    complete = not any(name.startswith("... ") for name in symbol_names)
    telemetry_only = complete and bool(symbol_names) and all(
        is_telemetry_name(name, path=False) for name in symbol_names)

    def scoring_path(path: str) -> bool:
        return not telemetry_only and is_scoring_path(path)

    checks = (
        (MODEL_CONFIG, "path", paths, is_model_side_path),
        (SCORING_LOGIC, "path", paths, scoring_path),
        (MODEL_CONFIG, "path", paths, is_model_config_path),
        (SCORING_LOGIC, "symbol", symbol_names, is_scoring_symbol if complete else is_scoring_name),
    )
    for cls, rule, names, test in checks:
        for name in names:
            if test(name):
                return cls, rule, name
    return UNKNOWN, None, None


def logic_class(paths: tuple[str, ...], symbol_names: tuple[str, ...]) -> str:
    """The class of :func:`logic_match`."""
    return logic_match(paths, symbol_names)[0]


def class_rank(name: str) -> int:
    return CLASSES.index(name)
