import json
from pathlib import Path

import nexus_runtime

DOC = Path(__file__).resolve().parents[1] / "docs" / "public-surface.json"


def test_public_surface_frozen():
    doc = json.loads(DOC.read_text(encoding="utf-8"))
    assert doc["schema"] == "nexus.runtime.public_surface.v1"
    assert sorted(nexus_runtime.__all__) == doc["init_all"]
    assert sorted(nexus_runtime.build_runtime_exports().names()) == doc["exports_names"]
