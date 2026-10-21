PYTHON ?= .venv/bin/python

.PHONY: install test check check-node
install: check-node
	python3 -m venv .venv
	.venv/bin/pip install -r requirements.lock
	npm ci --omit=dev --ignore-scripts

check-node:
	node -e 'const v = require("fs").readFileSync(".node-version", "utf8").trim(); require("assert").strictEqual(process.versions.node, v, "Use Node " + v); require("assert").strictEqual(require("./package.json").engines.node, v)'

test: check-node
	$(PYTHON) -m unittest discover -s tests -v
	npm test

check: check-node
	$(PYTHON) -m compileall -q newspaper
	node --check scripts/fetch.mjs
	node --check scripts/search.mjs
	node --check scripts/network.mjs
	node --check scripts/image.mjs
	node --check static/site.js
	$(PYTHON) scripts/check_profile.py
