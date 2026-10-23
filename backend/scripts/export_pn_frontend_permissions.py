"""Keep the UI's closed vocabulary derived from the authority's actual registry."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.authz.pn_resources import CATALOG_ACTIONS, PN_RESOURCES


def exports():
    return {
        "pnResources.json": {key: {"actions": value["actions"], "fields": value["fields"]}
                             for key, value in PN_RESOURCES.items()},
        "pnActions.json": CATALOG_ACTIONS,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    target = Path(__file__).resolve().parents[2] / "frontend/src/authz"
    for name, value in exports().items():
        content = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        path = target / name
        if args.check:
            if not path.exists() or path.read_text() != content:
                raise SystemExit(f"Stale PN permission vocabulary: {name}")
        else:
            path.write_text(content)
