.DEFAULT_GOAL := help
UV ?= uv
# The GitHub login of the repository owner, whose noreply address is a valid commit identity:
# the owner segment of the origin remote, as the Hygiene workflow's github.repository_owner is.
OWNER_LOGIN ?= $(shell git remote get-url origin 2>/dev/null | sed -nE 's,^(https://github\.com/|git@github\.com:)([^/]+)/.*,\2,p')

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

.PHONY: help setup unhide-pth fmt lint typecheck test test-all eval corpus-check corpus-split corpus-fetch sec-fsds clean docs-check label-pages eval-locate eval-convert eval-structure eval-mapping eval-gates dry-run expected-drafts eval-extraction verify-expected dev docker-up docker-health ci-workflows-check container-smoke hygiene-check

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

typecheck: ## Type check the packages, apps, the analytics golden test, eval harness and CI scripts
	@$(MAKE) --no-print-directory unhide-pth
	$(UV) run mypy packages $(wildcard apps) tests/analytics tests/test_cache_guard.py tests/test_fail_on_skip.py cache_isolation.py
	$(UV) run mypy conftest.py
	$(UV) run mypy eval/harness
	$(UV) run mypy $(wildcard scripts/ci/*.py)

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

eval-locate: ## Score the locator (TARGET=golden, dev, or train = the fit and validation parts, never the holdout; model_test needs CHECKPOINT=1)
	@$(MAKE) --no-print-directory unhide-pth
	PYTHONPATH=eval $(UV) run python -m harness.locate $(or $(TARGET),golden) $(if $(filter 1,$(CHECKPOINT)),--checkpoint) $(if $(LIMIT),--limit $(LIMIT))

eval-convert: ## Convert the golden set, a child process per document; records time and peak memory
	@$(MAKE) --no-print-directory unhide-pth
	PYTHONPATH=eval $(UV) run python -m harness.convert $(if $(ONLY),--only $(ONLY)) $(if $(FRESH),--no-cache)

eval-structure: ## Structure the golden set: statements, scale and currency, identity, language pairs
	@$(MAKE) --no-print-directory unhide-pth
	PYTHONPATH=eval $(UV) run python -m harness.structure

dry-run: ## One look at the train holdout through the whole pipeline; logged, spent once per candidate, no pool argument; EARLIER=path compares with an earlier run's report
	@$(MAKE) --no-print-directory unhide-pth
	PYTHONPATH=eval $(UV) run python -m harness.dry_run $(if $(EARLIER),--earlier $(EARLIER))

expected-drafts: ## Draft expected files from the extraction (ONLY=id; keeps checked or confirmed files)
	@$(MAKE) --no-print-directory unhide-pth
	PYTHONPATH=eval $(UV) run python -m harness.expected $(if $(ONLY),--only $(ONLY))

eval-mapping: ## Label mapping: TARGET=golden (default) the critical items and the expected files; TARGET=fit [LIMIT=n] unmapped labels by failure class
	@$(MAKE) --no-print-directory unhide-pth
	PYTHONPATH=eval $(UV) run python -m harness.mapping $(or $(TARGET),golden) $(if $(LIMIT),--limit $(LIMIT))

eval-gates: ## Gate A and Gate B over the golden set (dev), per period kind; marked not yet measured on model_test
	@$(MAKE) --no-print-directory unhide-pth
	PYTHONPATH=eval $(UV) run python -m harness.gates

verify-expected: ## Independent evidence for every figure of the expected files (Mac, Vision OCR)
	@$(MAKE) --no-print-directory unhide-pth
	PYTHONPATH=eval $(UV) run python -m harness.verify_expected

eval-extraction: ## Score the extraction against eval/golden/expected (OCR=ocrmac|tesseract|none, LABEL=name, FRESH=1 try one engine)
	@$(MAKE) --no-print-directory unhide-pth
	PYTHONPATH=eval $(UV) run python -m harness.extraction $(if $(OCR),--ocr $(OCR)) $(if $(LABEL),--label $(LABEL)) $(if $(FRESH),--fresh)

corpus-check: ## Validate the corpus pool split (no network)
	@$(MAKE) --no-print-directory unhide-pth
	$(UV) run python scripts/corpus.py check

corpus-split: ## Fit, validation and holdout of the train pool, as hashed and after the recorded moves
	@$(MAKE) --no-print-directory unhide-pth
	PYTHONPATH=eval $(UV) run python scripts/corpus.py split

corpus-fetch: ## Download, measure and dedupe the corpus into var/corpus (NEW=1: only documents not yet measured)
	@$(MAKE) --no-print-directory unhide-pth
	$(UV) run python scripts/corpus.py fetch $(if $(filter 1,$(NEW)),--new)

sec-fsds: ## SEC statement labels for training (needs FRA_SEC_USER_AGENT; QUARTERS=8)
	@$(MAKE) --no-print-directory unhide-pth
	$(UV) run python training/sources/sec_fsds.py --last $(or $(QUARTERS),8)

dev: ## Serve the API natively (profile native) on FRA_API_PORT
	@$(MAKE) --no-print-directory unhide-pth
	FRA_PROFILE=native $(UV) run uvicorn fra_api.main:app --host 127.0.0.1 --port $(FRA_API_PORT)

docker-up: ## Build and start the Docker profile (api and worker); fails if a service exits at start or the api does not turn healthy
	docker compose up -d --build --wait

docker-health: ## Check the Docker profile's API from the host: status ok, profile docker
	@$(MAKE) --no-print-directory unhide-pth
	$(UV) run python -m fra_api.healthcheck $(FRA_API_PORT) --profile docker

ci-workflows-check: ## Validate GitHub Actions workflows with actionlint
	bash scripts/ci/check-workflows.sh

hygiene-check: ## Check the commits not on origin/main, and the branch name, against the attribution rules
	@test -n "$(OWNER_LOGIN)" || { echo "hygiene-check: cannot read the owner login from the origin remote URL; pass OWNER_LOGIN=<login>"; exit 1; }
	python3 scripts/ci/hygiene.py --range origin/main..HEAD --branch "$$(git branch --show-current)" --owner-login $(OWNER_LOGIN)

container-smoke: ## Build and smoke-test the Docker API and placeholder worker
	bash scripts/ci/container-smoke.sh

clean: ## Remove caches and build artifacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .mypy_cache

docs-check: ## Fail on TBD placeholders left in docs
	@! grep -rniE "\bTBD\b" docs eval/golden/README.md \
		|| { echo "docs-check failed"; exit 1; }
	@echo "docs-check passed"
