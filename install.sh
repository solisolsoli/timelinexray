#!/bin/sh
# TimelineXray installer (POSIX sh). One command:
#
#   curl -fsSL https://raw.githubusercontent.com/solisolsoli/timelinexray/main/install.sh | sh
#
# Inspect first:  curl -fsSLO <url> && less install.sh && sh install.sh
# Options and exit codes: sh install.sh --help, and docs/install.md.
#
# Everything lives in functions and `main "$@"` is the last line, so a partially
# downloaded script never runs. The installer never edits shell rc files, sends no
# telemetry and uses the network only through the tool that installs the package spec
# (uv, pipx, pip or git).

set -eu

REPO_URL="https://github.com/solisolsoli/timelinexray"
NEXT_PIN="77d431aabf409ca1c1eed9bec7e2183f7c914e23"

EX_USAGE=2
EX_OS=10
EX_PYTHON=11
EX_SQLITE=12
EX_GIT=13
EX_TOOL=14
EX_INSTALL=15
EX_VERIFY=16

say() {
    printf '%s\n' "$*"
}

warn() {
    printf 'txray-install: %s\n' "$*" >&2
}

die() {
    # die CODE MESSAGE [HINT...]
    die_code=$1
    shift
    printf 'txray-install: error: %s\n' "$1" >&2
    shift
    for die_hint in "$@"; do
        printf '  %s\n' "$die_hint" >&2
    done
    exit "$die_code"
}

usage() {
    cat <<'EOF'
Install TimelineXray (the txray command and its read-only MCP server).

Usage: sh install.sh [options]

Options:
  --method auto|uv|pipx|venv  how to install (default auto: uv if on PATH, else pipx,
                              else a private virtual environment)
  --ref REF                   git branch, tag or commit to install (default main)
  --source PATH               install from a local checkout directory or a wheel file
                              instead of GitHub (a wheel is installed without an index)
  --uninstall                 remove what this installer installed
  --dry-run                   print every command and change nothing
  -h, --help                  show this help

Environment:
  TXRAY_INSTALL_METHOD, TXRAY_INSTALL_REF, TXRAY_INSTALL_SOURCE   defaults for the options
  TXRAY_PYTHON    the Python interpreter to use (default: python3, python3.13,
                  python3.12, python3.11, first one that is new enough)
  TXRAY_HOME      venv method: data directory (default ${XDG_DATA_HOME:-$HOME/.local/share}/timelinexray)
  TXRAY_BIN_DIR   venv method: where the txray link goes (default $HOME/.local/bin)

Requirements: Linux or macOS, Python 3.11+ with SQLite FTS5 and the trigram tokenizer,
git 2.38+. Windows is not supported.

Exit codes: 0 success; 2 usage error; 10 unsupported OS; 11 Python missing or older than
3.11; 12 no SQLite FTS5 trigram in the Python; 13 git missing or older than 2.38; 14 the
requested install tool is missing; 15 the installation or the uninstall failed; 16 the installed txray did
not run.

The installer never edits shell rc files. If the bin directory is not on PATH it prints
the line to add.
EOF
}

# -- helpers -------------------------------------------------------------------------

run() {
    # run COMMAND [ARG...]: print it; execute it unless --dry-run
    if [ "$DRY_RUN" = 1 ]; then
        say "[dry-run] $*"
        return 0
    fi
    say "+ $*"
    "$@" </dev/null
}

have() {
    command -v "$1" >/dev/null 2>&1
}

on_path() {
    case ":$PATH:" in
        *":$1:"*) return 0 ;;
    esac
    return 1
}

# -- environment ---------------------------------------------------------------------

parse_args() {
    METHOD=${TXRAY_INSTALL_METHOD:-auto}
    REF=${TXRAY_INSTALL_REF:-main}
    SOURCE=${TXRAY_INSTALL_SOURCE:-}
    UNINSTALL=0
    DRY_RUN=0
    while [ $# -gt 0 ]; do
        case $1 in
            --method)
                [ $# -ge 2 ] || die "$EX_USAGE" "--method needs a value (auto, uv, pipx or venv)"
                METHOD=$2
                shift 2
                ;;
            --method=*) METHOD=${1#--method=}; shift ;;
            --ref)
                [ $# -ge 2 ] || die "$EX_USAGE" "--ref needs a value (a branch, tag or commit)"
                REF=$2
                shift 2
                ;;
            --ref=*) REF=${1#--ref=}; shift ;;
            --source)
                [ $# -ge 2 ] || die "$EX_USAGE" "--source needs a path (a checkout directory or a wheel file)"
                SOURCE=$2
                shift 2
                ;;
            --source=*) SOURCE=${1#--source=}; shift ;;
            --uninstall) UNINSTALL=1; shift ;;
            --dry-run) DRY_RUN=1; shift ;;
            -h | --help) usage; exit 0 ;;
            *) die "$EX_USAGE" "unknown option: $1" "run 'sh install.sh --help' for the options" ;;
        esac
    done
    case $METHOD in
        auto | uv | pipx | venv) ;;
        *) die "$EX_USAGE" "unknown method '$METHOD'" "use one of: auto, uv, pipx, venv" ;;
    esac
    [ -n "$REF" ] || die "$EX_USAGE" "the ref must not be empty"
    # HOME only provides the defaults of TXRAY_HOME and TXRAY_BIN_DIR
    if [ -z "${TXRAY_HOME:-}" ] || [ -z "${TXRAY_BIN_DIR:-}" ]; then
        [ -n "${HOME:-}" ] || die "$EX_USAGE" "HOME is not set" "set HOME, or set TXRAY_HOME and TXRAY_BIN_DIR"
    fi
    DATA_HOME=${TXRAY_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/timelinexray}
    VENV_BIN_DIR=${TXRAY_BIN_DIR:-$HOME/.local/bin}
    # the txray link is a symbolic link to a path inside DATA_HOME: a relative path would be
    # read relative to the link's directory and dangle, so both are made absolute
    case $DATA_HOME in /*) ;; *) DATA_HOME=$PWD/$DATA_HOME ;; esac
    case $VENV_BIN_DIR in /*) ;; *) VENV_BIN_DIR=$PWD/$VENV_BIN_DIR ;; esac
}

check_os() {
    os=$(uname -s 2>/dev/null || echo unknown)
    case $os in
        Linux | Darwin) say "os: $os" ;;
        MINGW* | MSYS* | CYGWIN* | Windows*)
            die "$EX_OS" "Windows is not supported ($os)" \
                "TimelineXray needs fcntl locks and SIGALRM; see docs/limits.md" \
                "use WSL (a Linux distribution on Windows) and run this installer there"
            ;;
        *)
            die "$EX_OS" "unsupported operating system: $os" \
                "only Linux and macOS are supported; see docs/limits.md"
            ;;
    esac
}

# -- preflight -----------------------------------------------------------------------

PY_VERSION_CODE='import sys; print("%d.%d.%d" % sys.version_info[:3])'
# The same probe as scripts/check_sqlite.py (\x27 is a single quote).
PY_FTS5_CODE='import sqlite3, sys
try:
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE VIRTUAL TABLE temp.txray_probe USING fts5(x, tokenize = \x27trigram\x27)")
    connection.execute("DROP TABLE temp.txray_probe")
except sqlite3.Error as exc:
    print("SQLite %s: %s" % (sqlite3.sqlite_version, exc))
    sys.exit(1)
print(sqlite3.sqlite_version)'

version_at_least() {
    # version_at_least "3.12.4" MAJOR MINOR
    vmajor=${1%%.*}
    vrest=${1#*.}
    vminor=${vrest%%.*}
    case $vmajor$vminor in
        *[!0-9]* | "") return 1 ;;
    esac
    [ "$vmajor" -gt "$2" ] || { [ "$vmajor" -eq "$2" ] && [ "$vminor" -ge "$3" ]; }
}

find_python() {
    PYTHON=
    seen_new_python=0
    tried=
    if [ -n "${TXRAY_PYTHON:-}" ]; then
        set -- "$TXRAY_PYTHON"  # one candidate, never split at spaces
        have "$TXRAY_PYTHON" || die "$EX_PYTHON" "TXRAY_PYTHON='$TXRAY_PYTHON' is not an executable" \
            "unset TXRAY_PYTHON, or point it at a Python 3.11+ interpreter"
    else
        set -- python3 python3.13 python3.12 python3.11
    fi
    for candidate in "$@"; do
        have "$candidate" || continue
        found=$(command -v "$candidate")
        found_version=$("$found" -c "$PY_VERSION_CODE" 2>/dev/null </dev/null) || found_version=
        tried="$tried $found (${found_version:-unusable})"
        version_at_least "$found_version" 3 11 || continue
        seen_new_python=1
        if probe=$("$found" -c "$PY_FTS5_CODE" 2>&1 </dev/null); then
            PYTHON=$found
            PYTHON_VERSION=$found_version
            say "python: $PYTHON ($PYTHON_VERSION, SQLite $probe with FTS5 and the trigram tokenizer)"
            return 0
        fi
        FTS5_PROBLEM="$found: $(printf '%s' "$probe" | tail -n 1)"
    done
    if [ "$seen_new_python" = 1 ]; then
        die "$EX_SQLITE" "no Python 3.11+ with SQLite FTS5 and the trigram tokenizer was found" \
            "the index needs SQLite 3.34+ built with FTS5; ${FTS5_PROBLEM}" \
            "use a Python build that has it (python.org installer, Homebrew python, uv python install, pyenv) and re-run with TXRAY_PYTHON=/path/to/python" \
            "see docs/install.md, section Troubleshooting"
    fi
    if [ -z "$tried" ]; then
        die "$EX_PYTHON" "Python 3.11 or newer was not found (looked for python3, python3.13, python3.12, python3.11)" \
            "install Python 3.11+ (macOS: brew install python; Debian/Ubuntu: apt install python3), or set TXRAY_PYTHON=/path/to/python"
    fi
    die "$EX_PYTHON" "Python 3.11 or newer is required; found:$tried" \
        "install Python 3.11+ and re-run, or set TXRAY_PYTHON=/path/to/python"
}

check_git() {
    have git || die "$EX_GIT" "git was not found on PATH (git 2.38 or newer is required)" \
        "install git (macOS: xcode-select --install or brew install git; Debian/Ubuntu: apt install git)"
    git_line=$(git --version 2>/dev/null </dev/null || true)
    git_version=$(printf '%s\n' "$git_line" | sed -n 's/^git version \([0-9][0-9]*\.[0-9][0-9]*\).*/\1/p')
    version_at_least "$git_version" 2 38 || die "$EX_GIT" \
        "git 2.38 or newer is required; found '${git_line:-no version output}'" \
        "upgrade git and re-run"
    say "git: $(command -v git) ($git_version)"
}

resolve_source() {
    SPEC=
    SOURCE_IS_WHEEL=0
    if [ -z "$SOURCE" ]; then
        SPEC="git+$REPO_URL@$REF"
        return 0
    fi
    if [ -d "$SOURCE" ]; then
        [ -f "$SOURCE/pyproject.toml" ] || die "$EX_USAGE" "--source '$SOURCE' is a directory without pyproject.toml" \
            "pass the root of a TimelineXray checkout or a wheel file"
        SPEC=$(cd "$SOURCE" && pwd)
    elif [ -f "$SOURCE" ]; then
        case $SOURCE in
            *.whl) ;;
            *) die "$EX_USAGE" "--source '$SOURCE' is a file but not a wheel (*.whl)" ;;
        esac
        SOURCE_IS_WHEEL=1
        SPEC=$(cd "$(dirname "$SOURCE")" && pwd)/$(basename "$SOURCE")
    else
        die "$EX_USAGE" "--source '$SOURCE' does not exist" \
            "pass a checkout directory or a wheel file"
    fi
}

choose_method() {
    if [ "$METHOD" != auto ]; then
        have_method=$METHOD
        case $have_method in
            uv | pipx)
                have "$have_method" || die "$EX_TOOL" "--method $have_method was requested but '$have_method' is not on PATH" \
                    "install it, or use --method auto / --method venv"
                ;;
        esac
        return 0
    fi
    if have uv; then
        METHOD=uv
    elif have pipx; then
        METHOD=pipx
    else
        METHOD=venv
    fi
}

# -- install -------------------------------------------------------------------------

bin_dir_for() {
    # the directory where the chosen method puts the txray link
    case $1 in
        uv)
            if bin=$(uv tool dir --bin 2>/dev/null </dev/null) && [ -n "$bin" ]; then
                printf '%s\n' "$bin"
            else
                [ -n "${XDG_BIN_HOME:-}" ] || need_home_for "uv's default bin directory"
                printf '%s\n' "${XDG_BIN_HOME:-$HOME/.local/bin}"
            fi
            ;;
        pipx)
            if bin=$(pipx environment --value PIPX_BIN_DIR 2>/dev/null </dev/null) && [ -n "$bin" ]; then
                printf '%s\n' "$bin"
            else
                need_home_for "pipx's default bin directory"
                printf '%s\n' "$HOME/.local/bin"
            fi
            ;;
        venv) printf '%s\n' "$VENV_BIN_DIR" ;;
    esac
}

need_home_for() {
    [ -n "${HOME:-}" ] || die "$EX_USAGE" "HOME is not set, so $1 is unknown" "set HOME"
}

install_uv() {
    if [ "$SOURCE_IS_WHEEL" = 1 ]; then
        run uv tool install --force --python "$PYTHON" --no-index "$SPEC"
    else
        run uv tool install --force --python "$PYTHON" "$SPEC"
    fi
}

install_pipx() {
    if [ "$SOURCE_IS_WHEEL" = 1 ]; then
        run pipx install --force --python "$PYTHON" --pip-args=--no-index "$SPEC"
    else
        run pipx install --force --python "$PYTHON" "$SPEC"
    fi
}

install_venv() {
    venv_dir=$DATA_HOME/venv
    run mkdir -p "$DATA_HOME" "$VENV_BIN_DIR" || return 1
    if ! run "$PYTHON" -m venv --clear "$venv_dir"; then
        warn "could not create the virtual environment $venv_dir"
        warn "on Debian/Ubuntu install the venv module: apt install python3-venv"
        return 1
    fi
    if [ "$SOURCE_IS_WHEEL" = 1 ]; then
        run "$venv_dir/bin/python" -m pip install --disable-pip-version-check --no-index "$SPEC" || return 1
    else
        run "$venv_dir/bin/python" -m pip install --disable-pip-version-check "$SPEC" || return 1
    fi
    run ln -sf "$venv_dir/bin/txray" "$VENV_BIN_DIR/txray" || return 1
}

do_install() {
    say "method: $METHOD"
    say "package: $SPEC"
    case $METHOD in
        uv) install_uv || die "$EX_INSTALL" "uv tool install failed" \
            "behind a proxy set HTTPS_PROXY; see docs/install.md, section Troubleshooting" ;;
        pipx) install_pipx || die "$EX_INSTALL" "pipx install failed" \
            "behind a proxy set HTTPS_PROXY; see docs/install.md, section Troubleshooting" ;;
        venv) install_venv || die "$EX_INSTALL" "pip install failed" \
            "behind a proxy set HTTPS_PROXY; see docs/install.md, section Troubleshooting" ;;
    esac
}

verify() {
    bindir=$(bin_dir_for "$METHOD")
    if [ "$DRY_RUN" = 1 ]; then
        say "[dry-run] $bindir/txray --version"
        say "[dry-run] nothing was installed or changed"
        return 0
    fi
    txray_bin=$bindir/txray
    [ -x "$txray_bin" ] || die "$EX_VERIFY" "the installation finished but $txray_bin does not exist" \
        "look for the txray command with: command -v txray"
    say "+ $txray_bin --version"
    out=$("$txray_bin" --version </dev/null 2>&1) || die "$EX_VERIFY" "'$txray_bin --version' failed: $out"
    say "$out"
    case $out in
        txray\ *) ;;
        *) die "$EX_VERIFY" "unexpected output from '$txray_bin --version': $out" ;;
    esac
    if ! on_path "$bindir"; then
        say ""
        say "$bindir is not on your PATH. Add it (the installer never edits shell files):"
        say "  export PATH=\"$bindir:\$PATH\""
    fi
    say ""
    say "TimelineXray is installed. Next step:"
    if "$txray_bin" setup --help >/dev/null 2>&1 </dev/null; then
        say "  txray setup"
    else
        say "  txray pin $NEXT_PIN && txray index 77d431a"
    fi
}

# -- uninstall -----------------------------------------------------------------------

uninstall_venv() {
    venv_dir=$DATA_HOME/venv
    link=$VENV_BIN_DIR/txray
    found=0
    if [ -L "$link" ]; then
        target=$(readlink "$link" 2>/dev/null || true)
        case $target in
            "$venv_dir"/*) found=1; run rm -f "$link" ;;
            *) warn "$link does not point into $venv_dir; leaving it alone" ;;
        esac
    fi
    if [ -d "$venv_dir" ]; then
        found=1
        run rm -rf "$venv_dir"
    fi
    if [ "$found" = 1 ] && [ -d "$DATA_HOME" ] && [ "$DRY_RUN" = 0 ]; then
        rmdir "$DATA_HOME" 2>/dev/null || true
    fi
    [ "$found" = 1 ]
}

do_uninstall() {
    removed=0
    failed=
    for candidate in uv pipx venv; do
        [ "$METHOD" = auto ] || [ "$METHOD" = "$candidate" ] || continue
        case $candidate in
            uv)
                have uv || continue
                if uv tool list 2>/dev/null </dev/null | grep -Eq '^timelinexray( |$)'; then
                    if run uv tool uninstall timelinexray; then
                        removed=1
                    else
                        failed="$failed uv"
                    fi
                fi
                ;;
            pipx)
                have pipx || continue
                if pipx list --short 2>/dev/null </dev/null | grep -Eq '^timelinexray( |$)'; then
                    if run pipx uninstall timelinexray; then
                        removed=1
                    else
                        failed="$failed pipx"
                    fi
                fi
                ;;
            venv)
                if uninstall_venv; then
                    removed=1
                fi
                ;;
        esac
    done
    if [ -n "$failed" ]; then
        for failed_tool in $failed; do
            warn "$failed_tool uninstall of timelinexray failed (the package is still installed)"
        done
        die "$EX_INSTALL" "the uninstall did not complete" \
            "run the failing command yourself to see why: uv tool uninstall timelinexray, or pipx uninstall timelinexray"
    fi
    if [ "$removed" = 1 ]; then
        say "TimelineXray was uninstalled. Pinned snapshots and findings are user data and were not touched."
    else
        say "nothing to uninstall (no uv tool, pipx app or venv install of timelinexray found)"
    fi
}

# -- main ----------------------------------------------------------------------------

main() {
    parse_args "$@"
    check_os
    if [ "$UNINSTALL" = 1 ]; then
        do_uninstall
        return 0
    fi
    find_python
    check_git
    resolve_source
    if [ -n "$SOURCE" ]; then
        say "source: $SPEC (--ref ignored)"
    fi
    choose_method
    do_install
    verify
}

main "$@"
