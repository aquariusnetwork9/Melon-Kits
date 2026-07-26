# Marks tests/ as a package so `python -m unittest discover -s tests -t .` imports
# them as tests.test_* with the repo root on sys.path, which is what lets each test
# do a flat `import tsutil` / `import writer` against the modules under test.
