"""plex-db-ex — the extended Plex metadata and affinity store.

This package is the only writer of the store. Every other consumer opens the
same SQLite file read-only, and the schema is the contract between them.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
