.DEFAULT_GOAL := help
UV ?= uv

.PHONY: help setup unhide-pth fmt lint typecheck test test-all eval corpus-check corpus-fetch sec-fsds clean docs-check label-pages eval-locate eval-convert

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

label-pages: ## Blind labelling sheets for the scanned golden documents (Mac, Vision OCR)
	@$(MAKE) --no-print-directory unhide-pth
	$(UV) run python scripts/label_statement_pages.py serve

eval-locate: ## Score the locator (TARGET=golden, or TARGET=train / dev; model_test needs CHECKPOINT=1)
	@$(MAKE) --no-print-directory unhide-pth
	$(UV) run python eval/harness/locate.py $(or $(TARGET),golden) $(if $(CHECKPOINT),--checkpoint)

eval-convert: ## Convert the golden set, a child process per document; records time and peak memory
	@$(MAKE) --no-print-directory unhide-pth
	$(UV) run python eval/harness/convert.py $(if $(ONLY),--only $(ONLY)) $(if $(FRESH),--no-cache)

corpus-check: ## Validate the corpus pool split (no network)
	$(UV) run python scripts/corpus.py check

corpus-fetch: ## Download, measure and dedupe the corpus into var/corpus
	$(UV) run python scripts/corpus.py fetch

sec-fsds: ## SEC statement labels for training (needs FRA_SEC_USER_AGENT; QUARTERS=8)
	$(UV) run python training/sources/sec_fsds.py --last $(or $(QUARTERS),8)

clean: ## Remove caches and build artifacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .mypy_cache

docs-check: ## Fail on TBD placeholders left in docs
	@! grep -rniE "\bTBD\b" docs eval/golden/README.md \
		|| { echo "docs-check failed"; exit 1; }
	@echo "docs-check passed"
