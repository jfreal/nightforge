-- The hosted Routines page and its alerts.
-- runs_total is kept on the job, so the page never has to count a job's whole history.
ALTER TABLE jobs ADD COLUMN runs_total INTEGER NOT NULL DEFAULT 0;
UPDATE jobs SET runs_total = (SELECT COUNT(*) FROM runs WHERE runs.job_id = jobs.id);

-- The problem last alerted for each job ("late", "failed", ...), so each change is sent once.
CREATE TABLE alerts (
  job_id     TEXT PRIMARY KEY,
  state      TEXT NOT NULL,
  changed_at TEXT NOT NULL
);

-- The page reads the last 7 days across every job, and the daily prune deletes by age.
CREATE INDEX runs_by_time ON runs (finished_at);
