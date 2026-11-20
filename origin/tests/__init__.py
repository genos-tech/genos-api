# Importing the package (every `manage.py test origin.tests...` does) turns on the patch-leak tripwire
# (MIP-1765): a test that leaves a module attribute patched fails by name at its own tearDown.
from origin.tests.patch_leak_tripwire import install

install()

# Real network connections fail the test that opens them, by name (MIP-1776).
from origin.tests.network_guard import install as install_network_guard  # noqa: E402

install_network_guard()
