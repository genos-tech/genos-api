"""Fixtures a developer tool builds OUTSIDE the test runner (MIP-1469).

Production code on purpose: a dev command may not import `origin/tests` (the image leaves it out,
`test_dockerignore`), so what both the tests and a tool need lives here, and the tests wrap it.
Nothing in this package may delete rows in bulk; a destructive helper belongs to a test base
class and must refuse to run outside the test database (`guards.require_test_database`).
"""
