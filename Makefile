.DEFAULT_GOAL := help

PY_DIR  := python
JS_DIR  := javascript

.PHONY: help

help: ## Show this help message
	@echo "Usage:"
	@awk 'BEGIN {FS = ":.*## ";} /^[a-zA-Z0-9_.-]+:.*## / {printf "  make %-14s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------
.PHONY: py-sync py-lint py-fmt py-test py-bench py-clean

py-sync: ## Sync Python dependencies
	cd $(PY_DIR) && uv sync

py-lint: ## Lint Python code
	cd $(PY_DIR) && uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/

py-fmt: ## Format Python code
	cd $(PY_DIR) && uv run ruff check --fix src/ tests/ && uv run ruff format src/ tests/

py-test: py-fmt ## Run Python tests
	cd $(PY_DIR) && uv run pytest tests/ -v -s --durations=0 --ignore=tests/test_benchmark.py

py-bench: py-fmt ## Run sync vs async benchmark
	cd $(PY_DIR) && uv run pytest tests/test_benchmark.py -v -s --durations=0

py-clean: ## Clean Python temporary and build artifacts
	rm -rf $(PY_DIR)/dist/ $(PY_DIR)/build/ $(PY_DIR)/.venv/ $(PY_DIR)/.pytest_cache/ $(PY_DIR)/.ruff_cache/
	find $(PY_DIR) -type d -name __pycache__ -prune -exec rm -rf {} +
	find $(PY_DIR) -type f \( -name '*.pyc' -o -name '*.pyo' -o -name '.coverage' -o -name 'coverage.xml' \) -delete
	rm -rf $(PY_DIR)/src/*.egg-info/

# ---------------------------------------------------------------------------
# JavaScript / TypeScript
# ---------------------------------------------------------------------------
.PHONY: js-lint js-fmt js-test js-clean

js-lint: ## Lint JavaScript/TypeScript code
	cd $(JS_DIR) && npm run lint

js-fmt: ## Format JavaScript/TypeScript code
	cd $(JS_DIR) && npm run fmt

js-test: js-fmt ## Run JavaScript tests
	cd $(JS_DIR) && npm test

js-clean: ## Clean JavaScript temporary and build artifacts
	rm -rf $(JS_DIR)/dist/ $(JS_DIR)/node_modules/ $(JS_DIR)/.vitest/ $(JS_DIR)/coverage/
	find $(JS_DIR) -type f \( -name '*.tsbuildinfo' -o -name '.eslintcache' \) -delete

# ---------------------------------------------------------------------------
# Combined
# ---------------------------------------------------------------------------
.PHONY: lint fmt clean check-version release-patch release-minor release-major dry-publish

lint: py-lint js-lint ## Run all linters

fmt: py-fmt js-fmt ## Run all formatters

clean: py-clean js-clean ## Clean all temporary/build artifacts

check-version: ## Check published package versions on PyPI and npm
	@pypi_ver=$$(curl -s https://pypi.org/pypi/async-xenapi/json | python3 -c "import sys,json; print(json.load(sys.stdin)['info']['version'])"); \
	npm_ver=$$(curl -s https://registry.npmjs.org/async-xenapi/latest | python3 -c "import sys,json; print(json.load(sys.stdin)['version'])"); \
	echo "PyPI  async-xenapi $$pypi_ver"; \
	echo "npm   async-xenapi $$npm_ver"

# ---------------------------------------------------------------------------
# Release — bump version, tag, and push to trigger publish workflow
# ---------------------------------------------------------------------------
CURRENT_VERSION := $(shell grep '^version' $(PY_DIR)/pyproject.toml | head -1 | sed 's/.*"\(.*\)"/\1/')
MAJOR := $(word 1,$(subst ., ,$(CURRENT_VERSION)))
MINOR := $(word 2,$(subst ., ,$(CURRENT_VERSION)))
PATCH := $(word 3,$(subst ., ,$(CURRENT_VERSION)))

release-patch: ## Bump patch version, commit, tag, and push
	$(eval NEW_VERSION := $(MAJOR).$(MINOR).$(shell echo $$(($(PATCH)+1))))
	@$(MAKE) _do_release NEW_VERSION=$(NEW_VERSION)

release-minor: ## Bump minor version, commit, tag, and push
	$(eval NEW_VERSION := $(MAJOR).$(shell echo $$(($(MINOR)+1))).0)
	@$(MAKE) _do_release NEW_VERSION=$(NEW_VERSION)

release-major: ## Bump major version, commit, tag, and push
	$(eval NEW_VERSION := $(shell echo $$(($(MAJOR)+1))).0.0)
	@$(MAKE) _do_release NEW_VERSION=$(NEW_VERSION)

_assert_clean:
	@status="$$(git status --porcelain)"; \
	if [ -n "$$status" ]; then \
		echo "Error: working tree is not clean. Commit or stash changes before release."; \
		git status --short; \
		exit 1; \
	fi

_do_release: _assert_clean
	@echo "Bumping version: $(CURRENT_VERSION) → $(NEW_VERSION)"
	@if git rev-parse -q --verify "refs/tags/v$(NEW_VERSION)" >/dev/null; then \
		echo "Error: local tag v$(NEW_VERSION) already exists."; \
		exit 1; \
	fi
	cd $(PY_DIR) && uv version $(NEW_VERSION)
	cd $(JS_DIR) && npm version $(NEW_VERSION) --no-git-tag-version
	git add $(PY_DIR)/pyproject.toml $(PY_DIR)/uv.lock $(JS_DIR)/package.json $(JS_DIR)/package-lock.json
	@if git diff --cached --quiet; then \
		echo "Error: no version changes staged; aborting release."; \
		exit 1; \
	fi
	git commit -m "chore: bump version to $(NEW_VERSION)"
	git tag v$(NEW_VERSION)
	git push origin main v$(NEW_VERSION)

# ---------------------------------------------------------------------------
# Dry publish — build both packages without publishing
# ---------------------------------------------------------------------------
dry-publish: ## Build both packages without publishing
	@echo "=== Python ==="
	cd $(PY_DIR) && uv build
	@echo ""
	@echo "=== JavaScript ==="
	cd $(JS_DIR) && npm install && npm run build && npm publish --dry-run
	@echo ""
	@echo "=== Dry run complete (nothing published) ==="
