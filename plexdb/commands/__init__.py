"""One module per `plexdb` subcommand, discovered by `plexdb.cli.build_parser`.

A module here exposes:

- `NAME: str` — its subcommand name, used by `register` and by the sweep.
- `register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None`,
  which adds its own subparser and points `set_defaults(func=...)` at a handler
  in the same module.
- `ORDER: int` — its position, which is both the order `plexdb --help` lists
  commands and the order `plexdb sweep` runs them (ADR-0014). The existing
  commands are spaced by ten in the order someone runs them — create the store,
  fill it, enrich it, publish it — so a new command picks a free number in the
  gap it belongs in without any other module changing. Equal numbers sort by
  module name.
- `SWEEP: plexdb.sweep.Step`, optionally — declaring the module takes part in
  `plexdb sweep`, and whether its failure ends the run. A module that does not
  declare it is not in the sweep at all.

Adding a command is one new file; nothing else in this package or in
`plexdb/cli.py` changes.
"""

from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType

__all__ = ["iter_command_modules"]


def iter_command_modules(package: ModuleType | None = None) -> list[ModuleType]:
    """Import and return every module in this package.

    Shared by `plexdb.cli` (which registers them) and `plexdb.sweep` (which
    runs the ones that opted in), so the two can never disagree about what a
    command is.
    """
    package = package or _self()
    return [
        importlib.import_module(info.name)
        for info in pkgutil.iter_modules(package.__path__, f"{package.__name__}.")
    ]


def _self() -> ModuleType:
    import plexdb.commands

    return plexdb.commands
