"""Regenerate `tests/contract/*.json` from live code.

    python3 tests/gen_contract_fixtures.py

The acceptance gate rebuilds the same responses and compares them to what this wrote.
So: run this only when you *intend* a response shape to change, and read the diff it
produces as the change to BrainConnect's consumer contract that it is.

A fixture that changed without a matching entry in docs/CONTRACT.md is a contract
break that nobody wrote down.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from contract_cases import CASES, FIXTURE_DIR, build  # noqa: E402


def main() -> int:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    changed = []
    for name in CASES:
        path = FIXTURE_DIR / f"{name}.json"
        new = json.dumps(build(name), indent=2, sort_keys=True) + "\n"
        old = path.read_text(encoding="utf-8") if path.exists() else None
        if old != new:
            path.write_text(new, encoding="utf-8")
            changed.append(path.name)
        print(f"  {'updated' if old != new else 'ok     '}  {path.name}")

    if changed:
        print(f"\n{len(changed)} fixture(s) changed. That is a change to the consumer "
              "contract:\n")
        subprocess.run(["git", "--no-pager", "diff", "--", str(FIXTURE_DIR)],
                       check=False)
        print("\nUpdate docs/CONTRACT.md to say what moved, and why.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
