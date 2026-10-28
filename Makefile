# Raccourcis du labo. Toutes les cibles renvoient vers scripts/, qui contient la
# logique : le Makefile n'est qu'une memoire de ce qui est possible.
.PHONY: help bootstrap keys token start up down status autostart demo test test-infra doctor logs audit clean

help:
	@echo "make bootstrap  Installation complete (venvs, cles, .env, modeles)"
	@echo "make keys       Regenere la paire de cles JWT (invalide les jetons en cours)"
	@echo "make token      Emet un jeton de test (ROLES=analyst SUB=demo)"
	@echo "make start      Demarre TOUTE la pile (commande unique, idempotente)"
	@echo "make status     Etat compact des services"
	@echo "make up         Demarre la pile complete (sans Open WebUI)"
	@echo "make down       Arrete la pile (ARGS=--all pour Ollama aussi)"
	@echo "make autostart  Installe le demarrage automatique au boot"
	@echo "make demo       Demonstration de bout en bout"
	@echo "make test       Tests unitaires (pytest)"
	@echo "make test-infra Verifie que la pile reelle fonctionne (ARGS=--full)"
	@echo "make doctor     Diagnostique la pile (ARGS=--fix pour reparer)"
	@echo "make logs       Suit les journaux applicatifs"
	@echo "make audit      Affiche les dix derniers evenements d'audit"

bootstrap:
	./scripts/bootstrap.sh

keys:
	./.venv/bin/python scripts/gen_keys.py --force

SUB ?= demo
ROLES ?= analyst
token:
	@./.venv/bin/python scripts/mint_token.py --sub $(SUB) --roles $(ROLES) --quiet

start:
	./scripts/start.sh $(ARGS)

up:
	./scripts/up.sh $(ARGS)

status:
	./scripts/status.sh

autostart:
	./scripts/install-autostart.sh $(ARGS)
ARGS ?=
down:
	./scripts/down.sh $(ARGS)

demo:
	./scripts/demo.sh

test:
	./.venv/bin/python -m pytest

test-infra:
	./scripts/test-infra.sh $(ARGS)

doctor:
	./scripts/doctor.sh $(ARGS)

logs:
	tail -f var/logs/*.log

audit:
	@test -f var/logs/audit.jsonl && tail -n 10 var/logs/audit.jsonl | ./.venv/bin/python -m json.tool --json-lines \
	  || echo "aucun evenement d'audit pour le moment"

clean:
	rm -rf var/run var/logs .pytest_cache
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
