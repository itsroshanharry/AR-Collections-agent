"""Write the API's OpenAPI schema for the frontend's generated client.

    uv run python -m ab_dashboard.export_openapi            # -> frontend/openapi.json
    cd frontend && npm run gen:api                          # -> src/api/schema.d.ts

The frontend's types come from this file, so a changed response shape fails the
frontend type check rather than a live demo. tests/test_console_serving.py fails when
the committed copy is out of date.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from shared.config import REPO_ROOT

DEFAULT_PATH = REPO_ROOT / "frontend" / "openapi.json"


def schema_text() -> str:
    from ab_dashboard.main import app

    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


def main(argv: list[str]) -> int:
    path = Path(argv[0]) if argv else DEFAULT_PATH
    path.write_text(schema_text(), encoding="utf-8")
    print(f"Wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
