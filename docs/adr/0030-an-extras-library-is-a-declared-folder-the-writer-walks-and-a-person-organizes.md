# ADR-0030: An extras library is a declared folder the writer walks and a person organizes

An extras library is one entry in `extras.json`, beside Plex DB-EX's decisions files: a root under `PLEX_SOURCE_ROOTS`, a type, fields of kind `text`, `int`, `year` or `list`, a layout template over those fields, and filename parsers. No code knows what a library holds — Music Videos and Archival Bumpers are two entries in that file, with no `kind` field, no MusicBrainz lookup and no built-in YouTube code. The writer walks every root on its own timer as well as in the nightly sweep.

A file dropped anywhere under a root is an unorganized extra: it waits in the Extras tab's queue and no consumer sees it. Saving its queue row marks it organized and moves and renames it to the layout template; a walk never moves an organized file. The first walk of a library with no rows marks everything it finds organized.

An extra's `item_id` is `fs:<fnv1a>` of the canonical path where the walk first saw it, and follows the content: a path whose SHA-256 matches a known file takes that file's id. Exact and near duplicates are one item with several files until a person splits them.

Parsers (regex rules on the file stem) and scrapers (executables beside `extras.json`, matched on `source_url`) propose field values and never overwrite a person's. Consumers read organized extras over Plex DB-EX's HTTP routes, never from the SQLite file.
