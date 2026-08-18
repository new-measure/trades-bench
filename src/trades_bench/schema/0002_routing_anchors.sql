CREATE TABLE _routing_anchors (
    anchor_id       INTEGER PRIMARY KEY,
    latitude        REAL NOT NULL,
    longitude       REAL NOT NULL,
    selection_cell  TEXT NOT NULL
);
