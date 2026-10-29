-- Jobs and the runs they report. `seq` orders runs by arrival, so the dashboard can pull
-- "everything after the last one I saw" without missing a run that finished out of order.
CREATE TABLE jobs (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  every_minutes INTEGER,
  grace_minutes INTEGER,
  does          TEXT NOT NULL DEFAULT '',
  runs_on       TEXT NOT NULL DEFAULT '',
  schedule      TEXT NOT NULL DEFAULT '',
  url           TEXT NOT NULL DEFAULT '',
  paused        INTEGER NOT NULL DEFAULT 0,
  registered    INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

CREATE TABLE runs (
  seq         INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id      TEXT NOT NULL REFERENCES jobs(id),
  run_id      TEXT NOT NULL,
  received_at TEXT NOT NULL,
  started_at  TEXT,
  finished_at TEXT NOT NULL,
  status      TEXT NOT NULL,
  summary     TEXT NOT NULL DEFAULT '',
  counts      TEXT NOT NULL DEFAULT '{}',
  links       TEXT NOT NULL DEFAULT '[]',
  details     TEXT NOT NULL DEFAULT '',
  source      TEXT NOT NULL DEFAULT '',
  UNIQUE (job_id, run_id)
);

CREATE INDEX runs_by_job_time ON runs (job_id, finished_at DESC);
