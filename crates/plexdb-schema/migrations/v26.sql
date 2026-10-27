-- Version 26 — filename parsers on an extras library (ADR-0030).
--
-- `parsers` is the library's `parsers` list from `extras.json`, validated by `fold-extras` and
-- stored as JSON text: `[{"match": <regex>, "set": {<field>: <template>}}]`. NULL is no
-- parsers. Additive; `reader_shape` stays.

ALTER TABLE extra_library ADD COLUMN parsers TEXT;
