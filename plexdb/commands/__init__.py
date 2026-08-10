"""One module per `plexdb` subcommand, discovered by `plexdb.cli.build_parser`.

A module here exposes `register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None`,
which adds its own subparser and points `set_defaults(func=...)` at a handler
in the same module. Adding a command is one new file; nothing else in this
package or in `plexdb/cli.py` changes.

It also exposes `ORDER: int`, its position in `plexdb --help`. The existing
commands are spaced by ten in the order someone runs them — create the store,
fill it, enrich it, publish it — so a new command picks a free number in the
gap it belongs in without any other module changing. Equal numbers sort by
module name.
"""
