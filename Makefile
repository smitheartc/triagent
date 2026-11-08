# Triagent shortcuts. Everything runs from the repo root; the server's Python lives in .venv.
#   make demo                 one incident live end to end (./demo.sh), SCENARIO=bad_optic for another
#   make score                scorer over all nine scenarios, RUNS=3 by default (RUNS=1 for a quick pass)
#   make help                 the full list

PY        := .venv/bin/python
ES_URL    ?= http://localhost:9200
KIBANA    ?= http://localhost:5601
SCENARIO  ?= pdu_overload
RUNS      ?= 3
PARALLEL  ?= 2

.PHONY: help setup es kibana demo demo-stop score test tools-check

help:
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-12s %s\n", $$1, $$2}'

$(PY): server/requirements.txt
	@if command -v uv >/dev/null; then \
		uv venv -q --allow-existing --python '>=3.10' .venv && uv pip install -q --python $(PY) -r server/requirements.txt pytest; \
	else \
		python3 -m venv .venv && $(PY) -m pip install -q --upgrade pip && $(PY) -m pip install -q -r server/requirements.txt pytest; \
	fi
	@touch $(PY)

setup: $(PY) ## make .venv with the server's dependencies (and pytest)
	@test -f server/.env || { cp server/.env.example server/.env; echo "wrote server/.env: set NVIDIA_API_KEY in it"; }

es: ## start Elasticsearch + Kibana in Docker and apply the dcsim templates
	docker compose -f escluster/docker-compose.yml up -d
	@until curl -fsS -m 3 $(ES_URL)/_cluster/health 2>/dev/null | grep -qE '"status":"(green|yellow)"'; do sleep 2; done
	python3 escluster/es_setup.py --es-url $(ES_URL) --verify

kibana: ## the two judge dashboards (needs the dal1 data that make score loads)
	python3 escluster/kibana_demo.py --kibana-url $(KIBANA)

demo: setup ## one incident live: reset the demo namespace, stream SCENARIO at 60x, follow it to the ticket comment
	./demo.sh $(SCENARIO)

demo-stop: ## stop what make demo started
	./demo.sh stop

score: setup ## score all nine scenarios (loads seed 7 into namespace dal1 if it is empty); RUNS, PARALLEL
	$(PY) server/scorer.py --runs $(RUNS) --parallel $(PARALLEL)

test: setup ## server unit tests (no network)
	cd server && ../$(PY) -m pytest -q

tools-check: setup ## every ES tool against every ticket in the cluster, no model calls
	$(PY) server/tools_check.py
