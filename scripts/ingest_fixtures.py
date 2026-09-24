from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib import request


def main() -> None:
    parser = argparse.ArgumentParser(description="Submit NurserySignal fixture signals to the API")
    parser.add_argument("--url", default=os.getenv("SIGNALS_API_URL", "http://localhost:3000"))
    parser.add_argument("--token", default=os.getenv("COGNITO_ID_TOKEN"))
    args = parser.parse_args()
    if not args.token:
        raise SystemExit("set COGNITO_ID_TOKEN or pass --token")

    fixtures = json.loads(Path("fixtures/signals.json").read_text(encoding="utf-8"))
    for fixture in fixtures:
        body = json.dumps(fixture).encode("utf-8")
        req = request.Request(
            f"{args.url.rstrip('/')}/signals",
            data=body,
            headers={"Authorization": f"Bearer {args.token}", "content-type": "application/json"},
            method="POST",
        )
        with request.urlopen(req) as response:
            print(response.status, response.read().decode("utf-8"))


if __name__ == "__main__":
    main()
