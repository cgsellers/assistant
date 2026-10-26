"""
Print the API's OpenAPI schema, for the portal's generated types.

    uv run python -m api.openapi > portal/src/api/openapi.json

The portal's TypeScript client is generated from this (`npm run api` in
portal/), so the front end cannot quietly disagree with the API about a
field's name or type: CI regenerates it and fails on any difference.
"""

import json

from api.main import app

if __name__ == "__main__":
    print(json.dumps(app.openapi(), indent=2, sort_keys=True))
