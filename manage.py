#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import sys


def main():
    """Run administrative tasks."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "apis.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()


x = [
    {
        "id": "f6b534cf-ba19-4eb5-89eb-332803a99d0f",
        "type": "paragraph",
        "props": {"textColor": "default", "textAlignment": "left", "backgroundColor": "default"},
        "content": [
            {
                "text": "In terms of the Compatibility Type, it's better to choose from the followings IMO.",
                "type": "text",
                "styles": {},
            }
        ],
        "children": [],
    },
    {
        "id": "c6cc25f7-edd1-469d-803a-389ed4472f5b",
        "type": "numberedListItem",
        "props": {"textColor": "default", "textAlignment": "left", "backgroundColor": "default"},
        "content": [{"text": "BACKWARD", "type": "text", "styles": {}}],
        "children": [
            {
                "id": "7c3506ec-1993-4e8a-a866-d6dfe7c74c37",
                "type": "numberedListItem",
                "props": {
                    "textColor": "default",
                    "textAlignment": "left",
                    "backgroundColor": "default",
                },
                "content": [
                    {
                        "text": "If we don't need the deleted fields, choose this.",
                        "type": "text",
                        "styles": {},
                    }
                ],
                "children": [],
            }
        ],
    },
    {
        "id": "34b2b53c-77a2-4e3e-abc4-5947733bccec",
        "type": "numberedListItem",
        "props": {"textColor": "default", "textAlignment": "left", "backgroundColor": "default"},
        "content": [{"text": "FULL", "type": "text", "styles": {}}],
        "children": [
            {
                "id": "f58fed55-7166-4c68-bd56-bf9ef6bd3efb",
                "type": "numberedListItem",
                "props": {
                    "textColor": "default",
                    "textAlignment": "left",
                    "backgroundColor": "default",
                },
                "content": [
                    {
                        "text": "If we need the deleted fields, choose this.",
                        "type": "text",
                        "styles": {},
                    }
                ],
                "children": [],
            }
        ],
    },
    {
        "id": "b743aa66-ff01-4904-9b07-3f329cbb4514",
        "type": "numberedListItem",
        "props": {"textColor": "default", "textAlignment": "left", "backgroundColor": "default"},
        "content": [{"text": "NONE", "type": "text", "styles": {}}],
        "children": [
            {
                "id": "5ccc08f9-6534-4396-b5e8-1625abb171f8",
                "type": "numberedListItem",
                "props": {
                    "textColor": "default",
                    "textAlignment": "left",
                    "backgroundColor": "default",
                },
                "content": [
                    {
                        "text": "If we don't care about the source schema, choose this.",
                        "type": "text",
                        "styles": {},
                    }
                ],
                "children": [],
            }
        ],
    },
    {
        "id": "0d6ef9b6-e8ce-4e3f-95c3-35524a4ec210",
        "type": "paragraph",
        "props": {"textColor": "default", "textAlignment": "left", "backgroundColor": "default"},
        "content": [],
        "children": [],
    },
]
