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

    a = [
        {
            "id": "bf18f509-922c-4014-b549-0053f82b5263",
            "type": "heading",
            "props": {
                "level": 3,
                "textColor": "default",
                "isToggleable": false,
                "textAlignment": "left",
                "backgroundColor": "default",
            },
            "content": [{"text": "🧾 Description", "type": "text", "styles": {}}],
            "children": [],
        },
        {
            "id": "36eb000e-572d-435a-8fd2-3ed63630b29b",
            "type": "paragraph",
            "props": {
                "textColor": "default",
                "textAlignment": "left",
                "backgroundColor": "default",
            },
            "content": [
                {"text": "What needs to be done?", "type": "text", "styles": {"code": true}}
            ],
            "children": [],
        },
        {
            "id": "20866650-b975-406c-8f7f-f6be41065a85",
            "type": "paragraph",
            "props": {
                "textColor": "default",
                "textAlignment": "left",
                "backgroundColor": "default",
            },
            "content": [],
            "children": [],
        },
        {
            "id": "0e84d911-34c7-4dcc-8b61-182df94469a1",
            "type": "heading",
            "props": {
                "level": 3,
                "textColor": "default",
                "isToggleable": false,
                "textAlignment": "left",
                "backgroundColor": "default",
            },
            "content": [{"text": "🪜 Motivation", "type": "text", "styles": {}}],
            "children": [],
        },
        {
            "id": "b1fc2513-dda9-4488-9f5d-24967e31e6e6",
            "type": "paragraph",
            "props": {
                "textColor": "default",
                "textAlignment": "left",
                "backgroundColor": "default",
            },
            "content": [
                {"text": "Why is this task needed?", "type": "text", "styles": {"code": true}}
            ],
            "children": [],
        },
        {
            "id": "6920d5fc-e16a-4818-81c4-5afd228ceb4a",
            "type": "paragraph",
            "props": {
                "textColor": "default",
                "textAlignment": "left",
                "backgroundColor": "default",
            },
            "content": [],
            "children": [],
        },
        {
            "id": "c0cdee0d-0600-4f9b-9ab0-bf747f68cca4",
            "type": "heading",
            "props": {
                "level": 3,
                "textColor": "default",
                "isToggleable": false,
                "textAlignment": "left",
                "backgroundColor": "default",
            },
            "content": [{"text": "🎯 Further Context", "type": "text", "styles": {}}],
            "children": [],
        },
        {
            "id": "435808d2-a32f-4b64-8fac-9e8c323c272e",
            "type": "paragraph",
            "props": {
                "textColor": "default",
                "textAlignment": "left",
                "backgroundColor": "default",
            },
            "content": [{"text": "Any other sharing?", "type": "text", "styles": {"code": true}}],
            "children": [],
        },
        {
            "id": "1d13bdd9-6ce0-4fcb-9bb1-afdb84379424",
            "type": "paragraph",
            "props": {
                "textColor": "default",
                "textAlignment": "left",
                "backgroundColor": "default",
            },
            "content": [],
            "children": [],
        },
        {
            "id": "9188821c-60c4-41f9-8a4e-21d769e8e4d7",
            "type": "codeBlock",
            "props": {"language": ""},
            "content": [
                {
                    "text": 'import { Socket } from "socket.io-client";\nimport { useState, useEffect, useRef } from "react";\nimport { Box } from "@mui/joy";\nimport { useColorScheme } from "@mui/joy/styles";\nimport { en } from "@blocknote/core/locales";\nimport { BlockNoteView } from "@blocknote/mantine";\nimport { codeBlock } from "@blocknote/code-block";\nimport "@blocknote/core/fonts/inter.css";\nimport "@blocknote/mantine/style.css";',
                    "type": "text",
                    "styles": {},
                }
            ],
            "children": [],
        },
        {
            "id": "a86507a8-503c-4b98-bf78-b365105b3b5c",
            "type": "paragraph",
            "props": {
                "textColor": "default",
                "textAlignment": "left",
                "backgroundColor": "default",
            },
            "content": [],
            "children": [],
        },
    ]
