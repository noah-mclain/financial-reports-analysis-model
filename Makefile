.DEFAULT_GOAL := help
UV ?= uv

.PHONY: help setup unhide-pth fmt lint typecheck test test-all eval corpus-check corpus-fetch clean docs-check

help: ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  %-12s %s\n", $$1, $$2}'

setup: ## Create the workspace environment
	$(UV) sync --all-packages
	@$(MAKE) --no-print-directory unhide-pth
	@echo "environment ready: .venv"

unhide-pth: ## Clear the macOS hidden flag that makes Python skip .pth files
	@# Python 3.11 and later silently ignore any .pth file carrying the macOS hidden flag.
	@# uv writes them hidden on this machine, which leaves the editable workspace packages
	@# out of sys.path and produces a bare "No module named fra_core". Idempotent.
	@chflags nohidden .venv/lib/python*/site-packages/*.pth 2>/dev/null || true

fmt: ## Format
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

lint: ## Lint without fixing
	$(UV) run ruff format --check .
	$(UV) run ruff check .

typecheck: ## Type check the packages
	@$(MAKE) --no-print-directory unhide-pth
	$(UV) run mypy packages $(wildcard apps)

test: ## Fast tests (no models, no OCR)
	@$(MAKE) --no-print-directory unhide-pth
	$(UV) run pytest -m "not slow"

test-all: ## Every test, including slow ones
	@$(MAKE) --no-print-directory unhide-pth
	$(UV) run pytest

eval: ## Accuracy report over the golden set
	$(UV) run python -m fra_eval.report

corpus-check: ## Validate the corpus pool split (no network)
	$(UV) run python scripts/corpus.py check

corpus-fetch: ## Download, measure and dedupe the corpus into var/corpus
	$(UV) run python scripts/corpus.py fetch

clean: ## Remove caches and build artifacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .mypy_cache

docs-check: ## Fail on TBD placeholders left in docs
	@! grep -rniE "\bTBD\b" docs eval/golden/README.md \
		|| { echo "docs-check failed"; exit 1; }
	@echo "docs-check passed"
