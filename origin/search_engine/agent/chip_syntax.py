"""How a body names a person or an object as a chip. No imports: `prompts.py`
builds its text at import time and the tools import this too."""

# Appended to every body parameter that takes chips, so the six tool
# schemas can't drift apart. Says where each id comes from, because the
# model has no other way to learn a user's UUID.
CHIP_SYNTAX_HINT = (
    " To @mention a person or #reference a Genos object as a clickable chip, "
    "write a link labelled with @ or #: `[@Ken](user:<user uuid>)`, "
    "`[@design](group:<id>)`, `[#MIP-708](task:<task id>)`, "
    "`[#Q3 launch](milestone:<id>)`, `[#Genos](project:<id>)`, "
    "`[#Spec](note:personal:<id>)` or `note:task:<id>`, "
    "`[#eng](chat:gm:<chat uuid>)` — or write a task's display id bare, "
    "`#PRJ-123`. Ids come from the read tools (team members, tasks, "
    "search); never guess one. A mentioned person is notified. The chip "
    "shows the object's real name; a token that doesn't resolve to "
    "something you can see stays plain text."
)
# What the in-app declarations say instead: the system prompt has the rest.
CHIP_POINTER = " Takes @mention and #reference chips: see CHIPS in the system prompt."
