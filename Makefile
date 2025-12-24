.PHONY: demo demo-graph test test-discover fmt lint

demo:
	python -m gismo.cli.main demo

demo-graph:
	python -m gismo.cli.main demo-graph

test:
	python -m unittest -v

test-discover:
	python -m unittest discover -s tests -p "test*.py" -v

fmt:
	python -m compileall gismo tests

lint:
	python -m py_compile $(shell git ls-files '*.py')
