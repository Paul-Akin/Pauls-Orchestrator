"""Personal AI orchestrator hub.

Importing any ``hub`` module pins LiteLLM to its bundled price/context table.
Without this, ``import litellm`` performs an HTTP GET to raw.githubusercontent.com
on every start (see docs/adr/0001). The hub makes no network calls except the
model calls you ask for.
"""

import os as _os

_os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
