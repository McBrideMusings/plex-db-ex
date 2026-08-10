"""One module per `plexdb` subcommand, discovered by `plexdb.cli.build_parser`.

A module here exposes `register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None`,
which adds its own subparser and points `set_defaults(func=...)` at a handler
in the same module. Adding a command is one new file; nothing else in this
package or in `plexdb/cli.py` changes.
"""
