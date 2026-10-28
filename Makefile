COMPOSE ?= docker compose
PYTHON ?= python
VENV ?= .venv
LABCTL ?= $(PYTHON) -m labctl

.PHONY: help venv install lint format test check up up-load down clean logs ps load certs smoke

help: ## Muestra esta ayuda
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

venv: ## Crea el entorno virtual e instala dependencias de desarrollo
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install -r requirements-dev.txt

lint: ## ruff check + ruff format --check
	ruff check .
	ruff format --check .

format: ## Formatea el código con ruff
	ruff format .
	ruff check --fix .

test: ## Corre los tests
	pytest

check: lint test ## Lint + tests

up: ## Levanta el stack (sin carga)
	$(COMPOSE) up -d --build

up-load: ## Levanta el stack con el generador de carga
	$(COMPOSE) --profile load up -d --build

load: ## Arranca solo el generador de carga
	$(COMPOSE) --profile load up -d loadgen

down: ## Baja el stack y borra volúmenes
	$(COMPOSE) --profile load down -v

ps: ## Estado de los contenedores
	$(COMPOSE) ps

logs: ## Logs de api, worker y nginx
	$(COMPOSE) logs -f --tail=100 api worker nginx

certs: ## Regenera un certificado válido
	$(COMPOSE) run --rm certgen valid

smoke: ## Healthcheck rápido a través de nginx
	curl -fsS --cacert certs/ca.crt https://localhost:8443/readyz

clean: ## Borra caches locales
	rm -rf .pytest_cache .ruff_cache .labctl
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
