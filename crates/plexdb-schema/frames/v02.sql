
CREATE TABLE frame_harvest (
    id       INTEGER PRIMARY KEY CHECK (id = 1),
    item_id  TEXT NOT NULL,
    since    TEXT NOT NULL
);
