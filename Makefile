PYTHON ?= python3

.PHONY: test
test:
	$(PYTHON) -c "import pathlib,py_compile; paths=list(pathlib.Path('.').glob('*.py'))+list(pathlib.Path('brokers').glob('*.py'))+list(pathlib.Path('tests').glob('*.py')); [py_compile.compile(str(p), doraise=True) for p in paths]"
	$(PYTHON) -m unittest discover -s tests -p 'test*.py' -v
