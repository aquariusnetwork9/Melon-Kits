# Package marker. Without it `python -m unittest discover -s tests -t .` cannot import
# them as tests.test_* with the app root on sys.path, which is what lets each test module
# import the flat modules (config, redact, ...) directly.
