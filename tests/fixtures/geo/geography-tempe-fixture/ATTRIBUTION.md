# Attribution

Map data © OpenStreetMap contributors, available under the Open Database License (ODbL 1.0). Extract via Geofabrik GmbH.

This directory is a **derived database** of OpenStreetMap data: the
address catalog, the zone assignment, the routing anchor set, and the
anchor distance matrix are all produced from a pinned Geofabrik extract
of Arizona (identified in `manifest.json` under `source`).

Under the Open Database License 1.0 a derived database must retain the
same license, so this artifact — and any bundle that ships it — is
licensed under ODbL 1.0. The full text is in `LICENSE`.

No OpenStreetMap contributor metadata (user names, user IDs, changeset
ids) is present: the build strips every tag except the address columns
published in `addresses/catalog.csv`. No real person is represented
anywhere in this artifact.
