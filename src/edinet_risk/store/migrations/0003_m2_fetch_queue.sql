-- edinet-risk SQLite schema, version 3 (plan §5, §6-2〜6-4; M2 batch 2 Step G).
-- Applied by store/migrate.py inside one transaction; user_version is set there.
--
-- fetches becomes the retry queue of `fetch`. SQLite cannot change a CHECK constraint in place, so the
-- table is rebuilt (existing rows are kept; nothing references fetches).
--   status   pending  selected, not attempted yet (or stopped by 401/403 before an answer)
--            ok       body saved and verified (Content-Type, ZIP + CRC, XBRL/PublicDoc/*.xbrl|.htm); sha256 recorded
--            retry    transient failure (429/5xx after backoff, network error); retried by the next `fetch`
--                     until api.fetch_max_attempts, then failed
--            failed   400/404, invalid body, or attempts exhausted; retried only with `fetch --retry-failed`
--            no_xbrl  xbrlFlag=0: never requested (§6-2; listed by monitoring)
--   error_kind  auth | not_found | transient | invalid (last failure; NULL after ok)
--   last_error  key-masked message
CREATE TABLE fetches_v3 (
    doc_id            TEXT NOT NULL REFERENCES documents(doc_id),
    type              INTEGER NOT NULL,   -- 1 = XBRL zip (the source of truth, §6-2)
    status            TEXT NOT NULL CHECK (status IN ('pending', 'ok', 'retry', 'failed', 'no_xbrl')),
    path              TEXT,
    sha256            TEXT,
    size              INTEGER,
    attempts          INTEGER NOT NULL DEFAULT 0,   -- API requests answered for this body (backoff retries inside one request not counted)
    last_error        TEXT,
    error_kind        TEXT CHECK (error_kind IN ('auth', 'not_found', 'transient', 'invalid')),
    last_http_status  INTEGER,
    last_attempt_at   TEXT,
    run_id            TEXT,               -- last `fetch` run that changed the row (no FK: runs may be pruned)
    fetched_at        TEXT,
    PRIMARY KEY (doc_id, type)
);
INSERT INTO fetches_v3 (doc_id, type, status, path, sha256, size, attempts, last_error, fetched_at)
    SELECT doc_id, type, status, path, sha256, size, attempts, last_error, fetched_at FROM fetches;
DROP TABLE fetches;
ALTER TABLE fetches_v3 RENAME TO fetches;
CREATE INDEX fetches_status ON fetches(status);
