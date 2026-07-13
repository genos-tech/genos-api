# Keep in lockstep with the frontend's `taskMeta.statuses` — these maps
# are the server-side mirror the v2 meta/table payloads are built from,
# and a status missing here KeyErrors /api/v2/task/meta/ (the "Blocked"
# rollout hit exactly that).
STATUS_COLOR_MAP = {
    "open": {"chipColor": "#0044c2", "textColor": "white"},
    "wip": {"chipColor": "#ff8c00ff", "textColor": "white"},
    "blocked": {"chipColor": "#e11d48", "textColor": "white"},
    "pending": {"chipColor": "#b900ff", "textColor": "white"},
    "closed": {"chipColor": "#1dc200", "textColor": "white"},
    "deleted": {"chipColor": "#ff2323", "textColor": "white"},
}

# Neutral gray for any status value outside the map. Callers must go
# through `status_color()` rather than indexing STATUS_COLOR_MAP
# directly — one out-of-vocabulary task row 500ing the whole
# /task/meta/ payload is exactly how the "Blocked" rollout broke.
DEFAULT_STATUS_COLOR = {"chipColor": "#6b7280", "textColor": "white"}


def status_color(status_label):
    """Color entry for a status label (case-insensitive), with a neutral
    fallback for unknown / empty values."""
    return STATUS_COLOR_MAP.get((status_label or "").lower(), DEFAULT_STATUS_COLOR)

PRIORITY_COLOR_MAP = {
    "minimal": {"chipColor": "#9CA3AF", "textColor": "white"},
    "low": {"chipColor": "#34D399", "textColor": "white"},
    "normal": {"chipColor": "#3B82F6", "textColor": "white"},
    "high": {"chipColor": "#F59E0B", "textColor": "white"},
    "critical": {"chipColor": "#EF4444", "textColor": "white"},
}

EFFORT_LEVEL_COLOR_MAP = {
    "minimal": {"chipColor": "#9CA3AF", "textColor": "white"},
    "low": {"chipColor": "#34D399", "textColor": "white"},
    "moderate": {"chipColor": "#3B82F6", "textColor": "white"},
    "high": {"chipColor": "#F59E0B", "textColor": "white"},
    "extensive": {"chipColor": "#EF4444", "textColor": "white"},
}
