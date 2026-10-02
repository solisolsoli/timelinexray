#!/usr/bin/env python3
"""Build the sdist and wheel, install the wheel into a fresh virtual environment, smoke-test.

Not part of the ``timelinexray`` package and never imported by it. This is the last step of
``make ci`` and the clean-install walk of the release checklist:

1. copy the working tree (without ``.git`` and build output) to a temporary directory;
2. create an isolated build environment and install the build requirement
   ``setuptools>=77`` into it **from PyPI** (the only download; TimelineXray itself never
   uses the network except its guarded ``git fetch``);
3. build the sdist and the wheel through the PEP 517 hooks of ``setuptools.build_meta``;
4. check their contents: license files, ``License-Expression: Apache-2.0``, only the
   ``timelinexray`` package in the wheel (no ``scripts/`` or ``tests/``), and no
   ``tests/`` in the sdist either (the suite runs only from a repository checkout);
5. create a second, fresh virtual environment and install the wheel with ``--no-index``
   (offline), then run the installed ``txray`` outside the source tree: ``--version`` and
   ``mcp tools --json``; with ``TXRAY_TEST_UPSTREAM`` also ``pin`` (``file://`` URL),
   ``manifest --summary`` and ``show`` of a golden citation, and with ``--full`` also
   ``index`` and ``search``;
6. run the one-command installer on the wheel, ``sh install.sh --method venv --source
   <wheel>`` with ``TXRAY_HOME`` and ``TXRAY_BIN_DIR`` in the temporary directory (offline),
   run the linked ``txray --version``, then ``sh install.sh --uninstall`` and check that
   everything it created is gone;
7. with ``TXRAY_TEST_UPSTREAM``, run the installer on a copy of the checkout (``--source
   <copy>``, a directory: pip builds it, which downloads ``setuptools>=77`` again), then
   the linked ``txray setup --commit 77d431a... --upstream file://$UPSTREAM --no-index
   --json`` (exit 0, the commit in the report), ``txray setup --print-mcp-config json``
   (its ``command`` is the linked txray) and ``install.sh --uninstall`` (link and venv
   gone). Step 5 also checks ``python -m timelinexray --version``.

Every command and its observed result is printed with local locations replaced by
``$WORK`` and ``$UPSTREAM``, so the transcript can be pasted into the release checklist.

    python scripts/build_check.py [--full] [--keep]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD_REQUIREMENT = "setuptools>=77"
UPSTREAM_COMMIT = "77d431aabf409ca1c1eed9bec7e2183f7c914e23"
GOLDEN = ("home-mixer/candidate_pipeline/phoenix_candidate_pipeline.rs", "322-330",
          "let sources:", "3cf0a5c059666a047f55d0724fb0dd9ddd870a9f95b7e81aa5c7da3b127e8683")
IGNORE = shutil.ignore_patterns(".git", "build", "dist", "*.egg-info", "__pycache__", ".venv",
                                "venv", "*.pyc")


class CheckFailed(RuntimeError):
    pass


class Walk:
    def __init__(self, work: Path, upstream: Path | None) -> None:
        self.work = work
        self.upstream = upstream
        self.steps: list[tuple[str, str]] = []

    def redact(self, text: str) -> str:
        for path, label in ((self.upstream, "$UPSTREAM"), (self.work, "$WORK")):
            if path is not None:
                for form in {str(path), os.path.realpath(path)}:
                    text = text.replace(form, label)
        return text

    def run(self, argv: list[str], *, env: dict[str, str] | None = None, cwd: Path | None = None,
            show: str | None = None, quiet: bool = False) -> str:
        start = time.perf_counter()
        proc = subprocess.run(argv, capture_output=True, text=True, env=env, cwd=cwd or self.work)
        seconds = time.perf_counter() - start
        shown = self.redact(show or " ".join(argv))
        if proc.returncode != 0:
            raise CheckFailed(f"{shown} failed (exit {proc.returncode}):\n"
                              f"{self.redact(proc.stdout[-2000:])}{self.redact(proc.stderr[-2000:])}")
        if not quiet:
            self.steps.append((shown, f"exit 0 in {seconds:.1f} s"))
            print(f"$ {shown}\n  exit 0 ({seconds:.1f} s)")
        return proc.stdout

    def observe(self, text: str) -> None:
        text = self.redact(text)
        self.steps.append(("", text))
        print("  " + text.replace("\n", "\n  "))


def _clean_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if key not in ("PYTHONPATH", "PYTHONHOME", "TXRAY_STORE", "TXRAY_FINDINGS",
                          "PIP_REQUIRE_VIRTUALENV")}
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    return env


def _bin(venv: Path, name: str) -> str:
    return str(venv / "bin" / name)


def _check_dists(dist: Path, version: str) -> tuple[Path, Path]:
    sdists = list(dist.glob("*.tar.gz"))
    wheels = list(dist.glob("*.whl"))
    if len(sdists) != 1 or len(wheels) != 1:
        raise CheckFailed(f"expected one sdist and one wheel, found {sdists + wheels}")
    sdist, wheel = sdists[0], wheels[0]
    base = f"timelinexray-{version}"
    with tarfile.open(sdist) as archive:
        names = set(archive.getnames())
    for required in ("LICENSE", "NOTICE", "pyproject.toml", "README.md",
                     "src/timelinexray/__init__.py", "src/timelinexray/cli.py"):
        if f"{base}/{required}" not in names:
            raise CheckFailed(f"the sdist lacks {required}")
    shipped_tests = sorted(n for n in names if n.startswith(f"{base}/tests/"))
    if shipped_tests:  # the suite reads the repository (goldens, schemas, docs, git): all or nothing
        raise CheckFailed(f"the sdist ships tests, which cannot run outside a checkout "
                          f"(MANIFEST.in prunes tests/): {shipped_tests[:3]}")
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        metadata = archive.read(f"{base}.dist-info/METADATA").decode("utf-8")
    info = f"{base}.dist-info/"
    stray = sorted(n for n in names if not (n.startswith("timelinexray/") or n.startswith(info)))
    if stray:
        raise CheckFailed(f"the wheel holds files outside the package: {stray[:5]}")
    for required in (f"{info}licenses/LICENSE", f"{info}licenses/NOTICE",
                     "timelinexray/__init__.py", "timelinexray/cli.py"):
        if required not in names:
            raise CheckFailed(f"the wheel lacks {required}")
    for line in ("License-Expression: Apache-2.0", f"Version: {version}",
                 "Requires-Python: >=3.11"):
        if line not in metadata.splitlines():
            raise CheckFailed(f"the wheel METADATA lacks {line!r}")
    if any(n.startswith(("scripts/", "tests/", "timelinexray/tests/")) for n in names):
        raise CheckFailed("the wheel contains scripts or tests")
    return sdist, wheel


def _check_install_script(walk: Walk, env: dict[str, str], wheel: Path, version: str) -> None:
    """Real install of the built wheel through ``install.sh`` (venv method), then uninstall."""
    home, bin_dir = walk.work / "installer-home", walk.work / "installer-bin"
    script = str(ROOT / "install.sh")
    install_env = {**env, "TXRAY_HOME": str(home), "TXRAY_BIN_DIR": str(bin_dir),
                   "TXRAY_PYTHON": sys.executable}
    for name in ("TXRAY_INSTALL_METHOD", "TXRAY_INSTALL_REF", "TXRAY_INSTALL_SOURCE"):
        install_env.pop(name, None)
    walk.run(["sh", script, "--method", "venv", "--source", str(wheel)], env=install_env,
             show=f"sh install.sh --method venv --source $WORK/dist/{wheel.name}"
                  "   # TXRAY_HOME and TXRAY_BIN_DIR in $WORK")
    link = bin_dir / "txray"
    if not link.is_symlink() or not str(link.resolve()).startswith(str(home.resolve())):
        raise CheckFailed(f"install.sh did not link txray into {bin_dir}")
    out = walk.run([str(link), "--version"], env=env, show="$WORK/installer-bin/txray --version").strip()
    if out != f"txray {version}":
        raise CheckFailed(f"the installed txray printed {out!r}, expected 'txray {version}'")
    walk.observe(f"install.sh: {out}")
    walk.run(["sh", script, "--uninstall", "--method", "venv"], env=install_env,
             show="sh install.sh --uninstall --method venv")
    left = [str(path) for path in (link, home / "venv") if path.exists() or path.is_symlink()]
    if left:
        raise CheckFailed(f"install.sh --uninstall left {left}")
    walk.observe("install.sh --uninstall removed the link and the venv")


def _check_setup_report(report: dict, commit: str) -> None:
    """The ``txray setup --json`` report must name the pinned commit."""
    found = (report.get("data") or {}).get("commit") if isinstance(report, dict) else None
    if found != commit:
        raise CheckFailed(f"txray setup reported commit {found!r}, expected {commit!r}")


def _check_mcp_config(config: dict, link: str) -> None:
    """The printed ``mcpServers`` snippet must start the linked txray, not a venv path."""
    try:
        command = config["data"]["config"]["mcpServers"]["timelinexray"]["command"]
    except (KeyError, TypeError):
        raise CheckFailed(f"txray setup --print-mcp-config json printed no command: {config!r}")
    if command != link:
        raise CheckFailed(f"the MCP command is {command!r}, expected the linked txray {link!r}")


def _check_checkout_install(walk: Walk, env: dict[str, str], upstream: Path, version: str) -> None:
    """``install.sh --source <copy of the checkout>``, then ``txray setup`` and uninstall."""
    copy = walk.work / "checkout-copy"
    shutil.copytree(ROOT, copy, ignore=IGNORE)
    home, bin_dir = walk.work / "checkout-home", walk.work / "checkout-bin"
    script = str(ROOT / "install.sh")
    install_env = {**env, "TXRAY_HOME": str(home), "TXRAY_BIN_DIR": str(bin_dir),
                   "TXRAY_PYTHON": sys.executable}
    for name in ("TXRAY_INSTALL_METHOD", "TXRAY_INSTALL_REF", "TXRAY_INSTALL_SOURCE"):
        install_env.pop(name, None)
    walk.run(["sh", script, "--method", "venv", "--source", str(copy)], env=install_env,
             show="sh install.sh --method venv --source $WORK/checkout-copy"
                  "   # a copy of the checkout; TXRAY_HOME and TXRAY_BIN_DIR in $WORK")
    link = bin_dir / "txray"
    if not link.is_symlink():
        raise CheckFailed(f"install.sh did not link txray into {bin_dir}")
    url = "file://" + str(upstream)
    store = walk.work / "setup-store"
    run_env = {**env, "TXRAY_ALLOW_FILE_URLS": url}
    report = json.loads(walk.run(
        [str(link), "setup", "--commit", UPSTREAM_COMMIT, "--upstream", url, "--store", str(store),
         "--no-index", "--json"], env=run_env,
        show=f"$WORK/checkout-bin/txray setup --commit {UPSTREAM_COMMIT} --upstream "
             "file://$UPSTREAM --store $WORK/setup-store --no-index --json"))
    _check_setup_report(report, UPSTREAM_COMMIT)
    walk.observe(f"txray setup: pinned {report['data']['commit']}")
    config = json.loads(walk.run(
        [str(link), "setup", "--print-mcp-config", "json", "--store", str(store), "--json"], env=run_env,
        show="$WORK/checkout-bin/txray setup --print-mcp-config json --store $WORK/setup-store --json"))
    _check_mcp_config(config, str(link))
    walk.observe("the MCP command is the linked txray ($WORK/checkout-bin/txray)")
    walk.run(["sh", script, "--uninstall", "--method", "venv"], env=install_env,
             show="sh install.sh --uninstall --method venv")
    left = [str(path) for path in (link, home / "venv") if path.exists() or path.is_symlink()]
    if left:
        raise CheckFailed(f"install.sh --uninstall left {left}")
    walk.observe("install.sh --uninstall removed the link and the venv of the checkout install")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--full", action="store_true", help="also index and search the upstream")
    parser.add_argument("--keep", action="store_true", help="keep the temporary directory")
    args = parser.parse_args(argv)
    version = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]["version"]
    upstream_env = os.environ.get("TXRAY_TEST_UPSTREAM")
    upstream = Path(upstream_env).resolve() if upstream_env else None
    work = Path(tempfile.mkdtemp(prefix="txray-build-check-"))
    walk = Walk(work, upstream)
    env = _clean_env()
    try:
        tree = work / "tree"
        shutil.copytree(ROOT, tree, ignore=IGNORE)
        build_venv, install_venv, dist = work / "build-venv", work / "install-venv", work / "dist"
        walk.run([sys.executable, "-m", "venv", str(build_venv)], env=env,
                 show=f"python{sys.version_info.major}.{sys.version_info.minor} -m venv $WORK/build-venv")
        walk.run([_bin(build_venv, "python"), "-m", "pip", "install", "--quiet", BUILD_REQUIREMENT],
                 env=env, show=f"$WORK/build-venv/bin/python -m pip install '{BUILD_REQUIREMENT}'"
                                "   # downloads from PyPI")
        setuptools_version = walk.run([_bin(build_venv, "python"), "-c",
                                       "import setuptools; print(setuptools.__version__)"],
                                      env=env, quiet=True).strip()
        walk.observe(f"setuptools {setuptools_version}")
        for hook in ("build_sdist", "build_wheel"):  # one process each: the hooks keep state
            code = f"import sys, setuptools.build_meta as m; print(m.{hook}(sys.argv[1]))"
            walk.run([_bin(build_venv, "python"), "-c", code, str(dist)], env=env, cwd=tree,
                     show=f"$WORK/build-venv/bin/python -c 'setuptools.build_meta.{hook}"
                          "($WORK/dist)'   # in a copy of the working tree")
        sdist, wheel = _check_dists(dist, version)
        walk.observe(f"built {sdist.name} ({sdist.stat().st_size} bytes) and {wheel.name} "
                     f"({wheel.stat().st_size} bytes); LICENSE and NOTICE included, "
                     "License-Expression: Apache-2.0, package files only")
        walk.run([sys.executable, "-m", "venv", str(install_venv)], env=env,
                 show=f"python{sys.version_info.major}.{sys.version_info.minor} -m venv $WORK/install-venv")
        walk.run([_bin(install_venv, "python"), "-m", "pip", "install", "--quiet", "--no-index",
                  "--no-deps", str(wheel)], env=env,
                 show=f"$WORK/install-venv/bin/python -m pip install --no-index --no-deps "
                      f"$WORK/dist/{wheel.name}")
        txray = _bin(install_venv, "txray")
        located = walk.run([_bin(install_venv, "python"), "-c",
                            "import timelinexray; print(timelinexray.__file__)"], env=env, quiet=True)
        # resolve both sides: on macOS the temporary directory is reached through the
        # /var -> /private/var symbolic link, so the textual prefixes can differ
        if not Path(located.strip()).resolve().is_relative_to(install_venv.resolve()):
            raise CheckFailed(f"timelinexray was imported from {located.strip()}, not the venv")
        out = walk.run([txray, "--version"], env=env, show="txray --version").strip()
        if out != f"txray {version}":
            raise CheckFailed(f"txray --version printed {out!r}, expected 'txray {version}'")
        walk.observe(out)
        module_out = walk.run([_bin(install_venv, "python"), "-m", "timelinexray", "--version"],
                              env=env, show="python -m timelinexray --version").strip()
        if module_out != f"txray {version}":
            raise CheckFailed(f"python -m timelinexray --version printed {module_out!r}, "
                              f"expected 'txray {version}'")
        walk.observe(module_out)
        _check_install_script(walk, env, wheel, version)
        tools = json.loads(walk.run([txray, "mcp", "tools", "--json"], env=env,
                                    show="txray mcp tools --json"))
        names = [tool["name"] for tool in tools["tools"]]
        size = len(json.dumps(tools, separators=(",", ":")))
        walk.observe(f"{len(names)} tools: {', '.join(names)} ({size} bytes compact)")
        pinned = json.loads((ROOT / "tests" / "mcp_tools_list.json").read_text("utf-8"))
        expected = [tool["name"] for tool in pinned["tools"]]
        if names != expected:
            raise CheckFailed(f"the installed txray lists MCP tools {names}; the pinned "
                              f"tests/mcp_tools_list.json lists {expected}")
        if upstream is not None:
            url = "file://" + str(upstream)
            store = work / "store"
            run_env = {**env, "TXRAY_ALLOW_FILE_URLS": url}
            common = ["--store", str(store)]
            walk.run([txray, "pin", UPSTREAM_COMMIT, "--upstream", url, *common], env=run_env,
                     show=f"txray pin {UPSTREAM_COMMIT} --upstream file://$UPSTREAM --store $WORK/store")
            summary = json.loads(walk.run([txray, "manifest", UPSTREAM_COMMIT[:7], "--summary",
                                           "--json", *common], env=run_env,
                                          show="txray manifest 77d431a --summary --json --store $WORK/store"))
            counts = summary["data"]["counts"]
            walk.observe(f"{counts['total']} paths: " + ", ".join(
                f"{k} {v}" for k, v in counts["classification"].items()))
            path, lines, anchor, expected = GOLDEN
            shown = json.loads(walk.run([txray, "show", UPSTREAM_COMMIT[:7], path, "--lines", lines,
                                         "--anchor", anchor, "--json", *common], env=run_env,
                                        show=f"txray show 77d431a {path} --lines {lines} "
                                             f"--anchor '{anchor}' --json --store $WORK/store"))
            data = shown["data"]
            if data["span_sha256"] != expected or data["anchor"]["verdict"] != "FOUND":
                raise CheckFailed(f"golden G01 span differs: {data['span_sha256']}")
            walk.observe(f"span sha256 {data['span_sha256']} (golden G01), anchor FOUND")
            if args.full:
                report = json.loads(walk.run([txray, "index", UPSTREAM_COMMIT[:7], "--json",
                                              *common], env=run_env,
                                             show="txray index 77d431a --json --store $WORK/store"))
                build = report["data"]["build"]
                walk.observe("index: " + json.dumps({k: build[k] for k in sorted(build)
                                                     if k in ("files", "symbols", "calls",
                                                              "lexical", "syntax")},
                                                    sort_keys=True))
                hits = json.loads(walk.run([txray, "search", UPSTREAM_COMMIT[:7], "PhoenixScorer",
                                            "--limit", "3", "--json", *common], env=run_env,
                                           show="txray search 77d431a PhoenixScorer --limit 3 "
                                                "--json --store $WORK/store"))
                result = hits["data"]
                first = result["hits"][0] if result["hits"] else None
                walk.observe(f"{result['total']} hits; first "
                             + (f"{first['path']}:{first['start_line']}" if first else "none"))
                if not result["total"]:
                    raise CheckFailed("search found nothing")
            _check_checkout_install(walk, env, upstream, version)
        else:
            walk.observe("TXRAY_TEST_UPSTREAM is not set: pin/show against the upstream and the "
                         "checkout install skipped")
        print(f"\nbuild check passed: timelinexray {version}")
        return 0
    except CheckFailed as exc:
        print(f"\nbuild check FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        if args.keep:
            print(f"kept {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
