.DEFAULT_GOAL := help
UV ?= uv

# Ports shared with compose.yaml. .env is the single source and is read here, not included: it
# holds blank lines, `#` comment lines and NAME=value lines (see its header), and anything else
# stops make. A value already in the environment, or given on the make command line, wins over
# the file, as it does for compose.
ENV_FILE := .env
ifeq ($(wildcard $(ENV_FILE)),)
$(error $(ENV_FILE) is missing: it holds FRA_API_PORT and FRA_LLM_PORT)
endif
ENV_BAD_LINE := $(shell grep -nvE '^([A-Z][A-Z0-9_]*=[A-Za-z0-9._:/-]*|[[:space:]]*|\#.*)$$' $(ENV_FILE) | head -1)
ifneq ($(ENV_BAD_LINE),)
$(error $(ENV_FILE):$(ENV_BAD_LINE) is not blank, a # comment or NAME=value with letters, digits and . _ : / - only; LF line endings)
endif
ENV_DUPLICATE := $(shell grep -oE '^[A-Z][A-Z0-9_]*=' $(ENV_FILE) | sort | uniq -d | head -1)
ifneq ($(ENV_DUPLICATE),)
$(error $(ENV_FILE) sets $(subst =,,$(ENV_DUPLICATE)) more than once)
endif
$(foreach assignment,$(shell grep -E '^[A-Z][A-Z0-9_]*=' $(ENV_FILE)),$(eval $(subst =, ?= ,$(assignment))))
ifeq ($(strip $(FRA_API_PORT)),)
$(error FRA_API_PORT is empty: set it in $(ENV_FILE))
endif
ifeq ($(strip $(FRA_LLM_PORT)),)
$(error FRA_LLM_PORT is empty: set it in $(ENV_FILE))
endif
export FRA_API_PORT FRA_LLM_PORT

.PHONY: help setup unhide-pth fmt lint typecheck test test-all eval corpus-check corpus-fetch sec-fsds clean docs-check label-pages eval-locate eval-convert eval-structure expected-drafts eval-extraction verify-expected dev docker-up docker-health

help: ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  %-14s %s\n", $$1, $$2}'

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

typecheck: ## Type check the packages and the eval harness
	@$(MAKE) --no-print-directory unhide-pth
	$(UV) run mypy packages $(wildcard apps)
	$(UV) run mypy eval/harness

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

eval-structure: ## Structure the golden set: statements, scale and currency, identity, language pairs
	@$(MAKE) --no-print-directory unhide-pth
	$(UV) run python eval/harness/structure.py

expected-drafts: ## Draft expected files from the extraction (ONLY=id; keeps checked or confirmed files)
	@$(MAKE) --no-print-directory unhide-pth
	$(UV) run python eval/harness/expected.py $(if $(ONLY),--only $(ONLY))

verify-expected: ## Independent evidence for every figure of the expected files (Mac, Vision OCR)
	@$(MAKE) --no-print-directory unhide-pth
	PYTHONPATH=eval $(UV) run python -m harness.verify_expected

eval-extraction: ## Score the extraction against eval/golden/expected (G1 counts checked files only)
	@$(MAKE) --no-print-directory unhide-pth
	PYTHONPATH=eval $(UV) run python -m harness.extraction

corpus-check: ## Validate the corpus pool split (no network)
	$(UV) run python scripts/corpus.py check

corpus-fetch: ## Download, measure and dedupe the corpus into var/corpus
	$(UV) run python scripts/corpus.py fetch

sec-fsds: ## SEC statement labels for training (needs FRA_SEC_USER_AGENT; QUARTERS=8)
	$(UV) run python training/sources/sec_fsds.py --last $(or $(QUARTERS),8)

dev: ## Serve the API natively (profile native) on FRA_API_PORT
	@$(MAKE) --no-print-directory unhide-pth
	FRA_PROFILE=native $(UV) run uvicorn fra_api.main:app --host 127.0.0.1 --port $(FRA_API_PORT)

docker-up: ## Build and start the Docker profile (api and worker); fails if a service exits at start or the api does not turn healthy
	docker compose up -d --build --wait

docker-health: ## Check the Docker profile's API from the host: status ok, profile docker
	@$(MAKE) --no-print-directory unhide-pth
	$(UV) run python -m fra_api.healthcheck $(FRA_API_PORT) --profile docker

clean: ## Remove caches and build artifacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .mypy_cache

docs-check: ## Fail on TBD placeholders left in docs
	@! grep -rniE "\bTBD\b" docs eval/golden/README.md \
		|| { echo "docs-check failed"; exit 1; }
	@echo "docs-check passed"
