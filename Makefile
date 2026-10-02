PYTHON ?= python3
# worker processes of the test runner: a number, or auto (min(cpu count, 4)); JOBS=1 is serial
JOBS ?= auto
export PYTHONPATH := src

# The GitHub workflow (.github/workflows/ci.yml) runs exactly `make ci`, which runs these
# steps in this order (scripts/check_workflows.py fails if this list, the ci target and the
# workflow disagree):
# ci-steps: lint hygiene test-strict build-check
#
#   lint         byte-compile src, tests and scripts; unused imports and private names;
#                check the workflow files
#   hygiene      repository hygiene, privacy, licensing and module-boundary tests
#   test-strict  the full suite in $(JOBS) worker processes; with TXRAY_TEST_UPSTREAM set,
#                any skipped test fails
#   build-check  sdist + wheel in an isolated venv (downloads setuptools>=77 from PyPI),
#                install into a fresh venv offline, run the installed txray

.PHONY: help pycheck test lint check hygiene test-strict build-check ci clean

help:
	@echo "make test         run the unittest suite (stdlib only, no network; JOBS=N or auto)"
	@echo "make lint         byte-compile src, tests and scripts; unused imports; check the CI workflow"
	@echo "make check        lint + test"
	@echo "make hygiene      hygiene, privacy, licensing and module-boundary tests"
	@echo "make test-strict  full suite; no skips allowed when TXRAY_TEST_UPSTREAM is set"
	@echo "make build-check  build sdist + wheel, install into a fresh venv, smoke-test"
	@echo "make ci           exactly what CI runs: lint hygiene test-strict build-check"
	@echo "make clean        remove caches and build output"

pycheck:
	@$(PYTHON) -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "Python >= 3.11 is required")'
	@$(PYTHON) scripts/check_sqlite.py

test: pycheck
	$(PYTHON) scripts/run_tests.py --jobs $(JOBS)

lint: pycheck
	$(PYTHON) -m compileall -q src tests scripts
	$(PYTHON) scripts/check_lint.py
	$(PYTHON) scripts/check_workflows.py

check: lint test

hygiene: pycheck
	$(PYTHON) -m unittest tests.test_repo_hygiene tests.test_licensing tests.test_internal_api

test-strict: pycheck
	$(PYTHON) scripts/run_tests.py --jobs $(JOBS)

build-check: pycheck
	$(PYTHON) scripts/build_check.py

ci: pycheck lint hygiene test-strict build-check

clean:
	rm -rf build dist src/*.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
