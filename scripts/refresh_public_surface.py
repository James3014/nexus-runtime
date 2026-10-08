#!/usr/bin/env python3
"""Regenerate docs/public-surface.json from the installed nexus_runtime."""
import json
from pathlib import Path

import nexus_runtime

DOC = Path(__file__).resolve().parents[1] / "docs" / "public-surface.json"


def main():
    doc = {
        "schema": "nexus.runtime.public_surface.v1",
        "init_all": sorted(nexus_runtime.__all__),
        "exports_names": sorted(nexus_runtime.build_runtime_exports().names()),
    }
    DOC.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
