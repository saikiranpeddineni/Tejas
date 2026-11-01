# Offline regression tests

Run from the repository root:

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
```

The suite uses deterministic fakes and the Python standard library. It does not
contact Yahoo or a broker, require credentials, download models, or write to the
repository database. Optional imports are stubbed when they are unavailable so
the same command works in a minimal offline Python environment.
