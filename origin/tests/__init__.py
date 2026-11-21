# Importing the package (every `manage.py test origin.tests...` does) turns on the patch-leak tripwire
# (MIP-1765): a test that leaves a module attribute patched fails by name at its own tearDown.
from origin.tests.patch_leak_tripwire import install

install()
