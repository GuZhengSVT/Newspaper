PYTHON ?= .venv/bin/python

.PHONY: install test check
install:
	python3 -m venv .venv
	.venv/bin/pip install -r requirements.lock
	npm ci --omit=dev --ignore-scripts

test:
	$(PYTHON) -m unittest discover -s tests -v
	npm test

check:
	$(PYTHON) -m compileall -q newspaper
	node --check scripts/fetch.mjs
	node --check scripts/search.mjs
	node --check scripts/network.mjs
	node --check scripts/image.mjs
	node --check static/site.js
	$(PYTHON) scripts/check_profile.py
