"""Strict per-application binary size guard, in addition to IDF subtype checks."""
import pathlib
import sys

if __name__ == "__main__":
    path, limit = pathlib.Path(sys.argv[1]), int(sys.argv[2], 0)
    size = path.stat().st_size
    if not 0 < size < limit:
        raise SystemExit(f"ERROR: {path.name}: {size} bytes must be strictly below {limit}")
    print(f"Strict size OK: {path.name}: {size} / {limit} bytes ({limit-size} bytes spare)")
