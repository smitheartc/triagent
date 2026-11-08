# Triagent shortcuts. Everything runs from the repo root; the server's Python lives in .venv.
#   make demo                 three incidents live end to end (./demo.sh), SCENARIO=bad_optic for one
#   make score                scorer over all nine scenarios, RUNS=3 by default (RUNS=1 for a quick pass)
#   make remote HOST=<brev>   Elasticsearch + Kibana on a Brev/SSH host, tunnelled to localhost (escluster/remote.sh)
#   make help                 the full list

PY        := .venv/bin/python
# .venv/bin/python is often a symlink (uv), so its mtime can't track installs; a stamp file does
STAMP     := .venv/.triagent-deps
ES_URL    ?= http://localhost:9200
KIBANA    ?= http://localhost:5601
# empty: demo.sh's three-incident default; SCENARIO=bad_optic for one, or any comma list
SCENARIO  ?=
RUNS      ?= 3
PARALLEL  ?= 2
INGEST    ?=
HOST      ?=
DOCKER    ?= docker
PORT      ?= 8080

.PHONY: help setup es kibana demo demo-stop score test tools-check server fake-server remote tunnel remote-status

help:
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-14s %s\n", $$1, $$2}'

$(STAMP): server/requirements.txt
	@if command -v uv >/dev/null; then \
		uv venv -q --allow-existing --python '>=3.10' .venv && uv pip install -q --python $(PY) -r server/requirements.txt pytest; \
	else \
		python3 -m venv .venv && $(PY) -m pip install -q --upgrade pip && $(PY) -m pip install -q -r server/requirements.txt pytest; \
	fi
	@touch $(STAMP)

setup: $(STAMP) ## make .venv with the server's dependencies (and pytest)
	@test -f server/.env || { cp server/.env.example server/.env; echo "wrote server/.env: set NVIDIA_API_KEY in it"; }

es: ## start Elasticsearch + Kibana in Docker and apply the dcsim templates (DOCKER=docker.exe from WSL without integration)
	$(DOCKER) compose -f escluster/docker-compose.yml up -d
	@until curl -fsS -m 3 $(ES_URL)/_cluster/health 2>/dev/null | grep -qE '"status":"(green|yellow)"'; do sleep 2; done
	python3 escluster/es_setup.py --es-url $(ES_URL) --verify

kibana: ## the two judge dashboards (needs the dal1 data that make score loads)
	python3 escluster/kibana_demo.py --kibana-url $(KIBANA)

demo: setup ## incidents live: reset the demo namespace, stream SCENARIO (default three) at 60x, follow each to its ticket comment
	./demo.sh $(SCENARIO)

demo-stop: ## stop what make demo started
	./demo.sh stop

score: setup ## score all nine scenarios; RUNS, PARALLEL; INGEST=1 reloads seed 7 into dal1 (a new or reset cluster)
	$(PY) server/scorer.py --runs $(RUNS) --parallel $(PARALLEL) $(if $(INGEST),--ingest)

test: setup ## server unit tests (no network)
	@$(PY) -c 'import pytest' 2>/dev/null || { command -v uv >/dev/null && uv pip install -q --python $(PY) pytest || $(PY) -m pip install -q pytest; }
	cd server && ../$(PY) -m pytest -q

server: setup ## the real server on PORT (8080), reloading on code changes; ticket comments go to jira_mock on :8082
	cd server && ../$(PY) -m uvicorn main:app --reload --host 0.0.0.0 --port $(PORT)

fake-server: setup ## scripted investigations on PORT for app work: no Elasticsearch or NVIDIA key needed
	cd server && ../$(PY) -m uvicorn fake_server:app --host 0.0.0.0 --port $(PORT)

remote: ## start ES + Kibana on HOST (a Brev instance) and tunnel them to localhost; leave it running
	@test -n "$(HOST)" || { echo "usage: make remote HOST=<brev instance or ssh host>"; exit 2; }
	escluster/remote.sh up $(HOST)

tunnel: ## only the tunnel to HOST's ES + Kibana (already running there)
	@test -n "$(HOST)" || { echo "usage: make tunnel HOST=<brev instance or ssh host>"; exit 2; }
	escluster/remote.sh tunnel $(HOST)

remote-status: ## containers and cluster health on HOST
	@test -n "$(HOST)" || { echo "usage: make remote-status HOST=<brev instance or ssh host>"; exit 2; }
	escluster/remote.sh status $(HOST)

tools-check: setup ## every ES tool against every ticket in the cluster, no model calls
	$(PY) server/tools_check.py
