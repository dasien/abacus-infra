.PHONY: test check gate
test:
	python3 -m unittest discover -s tests -t .
check: test
gate: test
