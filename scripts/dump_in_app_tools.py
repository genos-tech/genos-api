"""Write the in-app agent's tool names, write tools and parameter names to JSON (MIP-2048).

`scripts/build_official_skills.py --tools-dump` reads it, to check that a translated runbook only
names tools and parameters the in-app agent has. Run it where Django runs (the api image):

    python manage.py shell -c "exec(open('scripts/dump_in_app_tools.py').read())"   # writes tools.json
"""

import json

import django

django.setup()
from origin.search_engine.agent.tools import base  # noqa: E402
from origin.views.public.mcp import tools as mcp  # noqa: E402

out = {
    "registry": sorted(base.REGISTRY),
    "write": sorted(n for n, t in base.REGISTRY.items() if t.requires_approval),
    "params": {
        n: sorted((t.parameters_schema or {}).get("properties", {}))
        for n, t in base.REGISTRY.items()
    },
    "mcp_only": sorted(base.MCP_ONLY_REGISTRY),
    "catalog": {t.name: t.registry_name for t in mcp.CATALOG},
}
with open("tools.json", "w") as f:
    json.dump(out, f, indent=1)
print("wrote tools.json:", len(out["registry"]), "in-app tools")
