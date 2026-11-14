"""Ingestion of CFPB complaint data into the `raw` schema.

Three sources, all published by the CFPB:

* ``api``        - the public search API, used for incremental daily loads by date_received.
* ``bulk``       - complaints.csv.zip, used for full or date-bounded backfills.
* ``narratives`` - the frozen Narratives Archive (FOIA reading room), the only place consumer
  narratives are still published after 2026-08-14.
"""
