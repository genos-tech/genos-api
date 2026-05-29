"""Unit tests for the BlockNote mention extractor.

Pure-Python — no DB fixtures required. We use `unittest.TestCase`
(rather than the project's `BaseAPITestCase` / Django's `SimpleTestCase`)
so this module imports cleanly even when the Django settings + DB
backend aren't bootstrappable (e.g. running locally without psycopg2
installed). The extractor itself has zero Django imports.
"""

import unittest

from origin.services.mention_extractor import (
    extract_all_mentions,
    extract_mention_group_ids,
    extract_mentioned_user_ids,
)


class ExtractMentionedUserIdsTests(unittest.TestCase):
    def test_empty_body_returns_empty_set(self):
        self.assertEqual(extract_mentioned_user_ids([]), set())
        self.assertEqual(extract_mentioned_user_ids(None), set())
        self.assertEqual(extract_mentioned_user_ids("not a list"), set())

    def test_extracts_single_mention(self):
        body = [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "hi "},
                    {
                        "type": "mention",
                        "props": {"userId": "u-alice", "userName": "Alice"},
                    },
                ],
            }
        ]
        self.assertEqual(extract_mentioned_user_ids(body), {"u-alice"})

    def test_extracts_multiple_mentions(self):
        body = [
            {
                "type": "paragraph",
                "content": [
                    {"type": "mention", "props": {"userId": "u-alice"}},
                    {"type": "text", "text": " and "},
                    {"type": "mention", "props": {"userId": "u-bob"}},
                ],
            }
        ]
        self.assertEqual(
            extract_mentioned_user_ids(body),
            {"u-alice", "u-bob"},
        )

    def test_duplicate_mentions_collapse_to_one(self):
        body = [
            {
                "type": "paragraph",
                "content": [
                    {"type": "mention", "props": {"userId": "u-alice"}},
                    {"type": "text", "text": " "},
                    {"type": "mention", "props": {"userId": "u-alice"}},
                ],
            }
        ]
        self.assertEqual(extract_mentioned_user_ids(body), {"u-alice"})

    def test_recurses_into_children(self):
        # BlockNote nests list items / quote contents as `children`.
        body = [
            {
                "type": "bulletListItem",
                "children": [
                    {
                        "type": "bulletListItem",
                        "content": [
                            {"type": "mention", "props": {"userId": "u-nested"}},
                        ],
                    }
                ],
            }
        ]
        self.assertEqual(extract_mentioned_user_ids(body), {"u-nested"})

    def test_recurses_into_nested_content(self):
        body = [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {"type": "mention", "props": {"userId": "u-deep"}},
                        ],
                    }
                ],
            }
        ]
        self.assertEqual(extract_mentioned_user_ids(body), {"u-deep"})

    def test_ignores_non_mention_nodes(self):
        body = [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "no mentions here"},
                    {"type": "link", "props": {"url": "https://example.com"}},
                ],
            }
        ]
        self.assertEqual(extract_mentioned_user_ids(body), set())

    def test_ignores_mention_with_missing_userId(self):
        body = [
            {
                "type": "paragraph",
                "content": [
                    {"type": "mention", "props": {"userName": "no id"}},
                    {"type": "mention"},  # no props
                ],
            }
        ]
        self.assertEqual(extract_mentioned_user_ids(body), set())

    def test_coerces_non_string_userId_to_string(self):
        # Defensive: if a client somehow sends a number, the set
        # contains the string version (matches the v3 UUID convention).
        body = [
            {
                "type": "paragraph",
                "content": [
                    {"type": "mention", "props": {"userId": 42}},
                ],
            }
        ]
        self.assertEqual(extract_mentioned_user_ids(body), {"42"})

    def test_malformed_input_does_not_crash(self):
        # Items that aren't dicts or lists should be silently skipped.
        body = ["string", 42, None, {"type": "mention", "props": {"userId": "u-1"}}]
        self.assertEqual(extract_mentioned_user_ids(body), {"u-1"})

    def test_ignores_mentionGroup_nodes(self):
        body = [
            {
                "type": "paragraph",
                "content": [
                    {"type": "mention", "props": {"userId": "u-direct"}},
                    {
                        "type": "mentionGroup",
                        "props": {"groupId": "5", "groupName": "design"},
                    },
                ],
            }
        ]
        # Only direct user mentions go in this bucket.
        self.assertEqual(extract_mentioned_user_ids(body), {"u-direct"})


class ExtractMentionGroupIdsTests(unittest.TestCase):
    def test_extracts_group_ids(self):
        body = [
            {
                "type": "paragraph",
                "content": [
                    {"type": "mentionGroup", "props": {"groupId": "5"}},
                    {"type": "mentionGroup", "props": {"groupId": 7}},
                ],
            }
        ]
        # Both string + int inputs collapse to string values.
        self.assertEqual(extract_mention_group_ids(body), {"5", "7"})

    def test_ignores_user_mentions(self):
        body = [
            {
                "type": "paragraph",
                "content": [
                    {"type": "mention", "props": {"userId": "u-alice"}},
                    {"type": "mentionGroup", "props": {"groupId": "9"}},
                ],
            }
        ]
        self.assertEqual(extract_mention_group_ids(body), {"9"})


class ExtractAllMentionsTests(unittest.TestCase):
    def test_returns_both_in_one_walk(self):
        body = [
            {
                "type": "paragraph",
                "content": [
                    {"type": "mention", "props": {"userId": "u-alice"}},
                    {"type": "mentionGroup", "props": {"groupId": "5"}},
                    {"type": "mention", "props": {"userId": "u-bob"}},
                ],
            }
        ]
        users, groups = extract_all_mentions(body)
        self.assertEqual(users, {"u-alice", "u-bob"})
        self.assertEqual(groups, {"5"})

    def test_empty_input(self):
        users, groups = extract_all_mentions([])
        self.assertEqual(users, set())
        self.assertEqual(groups, set())
