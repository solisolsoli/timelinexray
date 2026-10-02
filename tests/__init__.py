"""TimelineXray test suite.

Importing this package blocks Python-level network access for the entire run, so any
accidental socket use fails loudly. Git fetches in the suite only use file:// URLs.
"""

from tests.support import block_network

block_network()
