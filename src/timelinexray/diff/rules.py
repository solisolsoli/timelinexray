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

CLASSIFIER_VERSION = 2

PARAMETER_DEFAULT = "parameter-default"
REGISTRATION = "registration"
SCORING_LOGIC = "scoring-logic"
MODEL_CONFIG = "model-config"
LICENSE = "license"
DOCS_ONLY = "docs-only"
TEST_ONLY = "test-only"
BUILD_DEPENDENCY = "build-dependency"
COSMETIC = "cosmetic"
GENERATED_VENDORED = "generated-vendored"
UNKNOWN = "unknown"

#: Every class, in reporting order.
CLASSES = (
    PARAMETER_DEFAULT,
    REGISTRATION,
    SCORING_LOGIC,
    MODEL_CONFIG,
    LICENSE,
    DOCS_ONLY,
    TEST_ONLY,
    BUILD_DEPENDENCY,
    COSMETIC,
    GENERATED_VENDORED,
    UNKNOWN,
)

CLASS_TITLES = {
    PARAMETER_DEFAULT: "Parameter default change",
    REGISTRATION: "Filter/pipeline registration change",
    SCORING_LOGIC: "Weight/scoring logic change",
    MODEL_CONFIG: "Model/config change",
    LICENSE: "License/notice change",
    DOCS_ONLY: "Documentation-only change",
    TEST_ONLY: "Test-only change",
    BUILD_DEPENDENCY: "Build/dependency/import change",
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
    SCORING_LOGIC: "changed code whose path or enclosing symbol names scoring, weights or "
    "ranking (a name heuristic, not a semantic analysis)",
    MODEL_CONFIG: "changed code or configuration in a model, feature, config, schema, proto, "
    "thrift, inference, train or training location (a path heuristic)",
    LICENSE: "a license or notice file, or license header lines, changed",
    DOCS_ONLY: "only documentation files changed",
    TEST_ONLY: "only test files or test regions changed (a path and file-name heuristic, "
    "plus test symbols such as #[cfg(test)] modules)",
    BUILD_DEPENDENCY: "a build or dependency manifest changed (a file-name heuristic: BUILD, "
    "*.bazel, Cargo.toml, build.rs, requirements*.txt, Makefile, ...), or every changed code "
    "line is an import, use, extern crate, bodyless mod or package declaration (Milestone 2 "
    "symbols)",
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
    MODEL_CONFIG: "that a model artifact was deployed",
    LICENSE: "a determination of the applicable license",
    DOCS_ONLY: "a change of behaviour",
    TEST_ONLY: "a change of production code",
    BUILD_DEPENDENCY: "that behaviour is unchanged",
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

_SCORING = re.compile(r"(?i)(scor|weight|rank|boost|penalt|decay|diversit|blend|multiplier|calibrat)")
_MODEL_CONFIG_DIRS = re.compile(
    r"(?i)^(models?|features?|configs?|conf|settings|schemas?|protos?|thrift|embeddings?|"
    r"checkpoints?|inference|train|training)$"
)
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


def is_scoring_name(text: str | None) -> bool:
    return bool(text) and bool(_SCORING.search(text))


def is_model_config_path(path: str) -> bool:
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

    Order: a scoring-related path, a model/config path, a scoring-related enclosing symbol
    name, else unknown. (A filtering/visibility name rule was tried for classifier version 2
    and rejected: in a hand check most of its items were caches, telemetry and tooling, so
    such changes stay ``unknown``; see ``docs/updates.md``.)
    """
    checks = (
        (SCORING_LOGIC, "path", paths, is_scoring_name),
        (MODEL_CONFIG, "path", paths, is_model_config_path),
        (SCORING_LOGIC, "symbol", symbol_names, is_scoring_name),
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
