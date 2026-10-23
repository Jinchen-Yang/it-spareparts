"""Export the implemented catalog surface without importing the host application."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI

from app.pn_catalog.http import router


def export() -> str:
    app = FastAPI(title="Shared PN Catalog", version="1.0.0-b3")
    app.include_router(router, prefix="/api")
    return json.dumps(app.openapi(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[2]
                        / "docs/design/shared-pn/openapi.json")
    args = parser.parse_args()
    content = export()
    if args.check:
        if not args.output.exists() or args.output.read_text() != content:
            raise SystemExit("PN catalog OpenAPI is stale; regenerate the committed contract")
    else:
        args.output.write_text(content)
