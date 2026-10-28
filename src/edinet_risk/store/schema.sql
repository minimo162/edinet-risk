-- edinet-risk SQLite schema, version 1 (plan §5; M1 minimal set).
-- Applied by store/migrate.py inside one transaction; user_version is set there.
-- Connection PRAGMAs (journal_mode=WAL, foreign_keys=ON) are set by store/db.py.
--
-- Conventions
--   * Timestamps: ISO-8601 text with +09:00 (JST, fixed offset; plan §3-1).
--   * *_json columns: JSON text (sorted keys) so that content hashes are stable (M1 acceptance 5).
--   * Derived tables (extracts/items/matches/sentence_changes) are keyed by processor version
--     and carry the hash of their input, so re-runs skip unchanged work (plan §6-3).
--   * Deferred to later migrations: population (M2, migrations/0002), llm_cache / llm_results (M3).

-- ---------------------------------------------------------------- run bookkeeping
CREATE TABLE runs (
    run_id          TEXT PRIMARY KEY,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    cmd             TEXT NOT NULL,
    fy              TEXT,                 -- e.g. '2026-03'
    as_of           TEXT,                 -- base date T (YYYY-MM-DD)
    meta_synced_at  TEXT,                 -- last doclist sync used by this run (§6-5)
    args_json       TEXT NOT NULL DEFAULT '{}',
    code_version    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running', 'ok', 'failed', 'aborted'))
);

-- Audit log of API calls. The key never reaches this table (params_masked is key-free).
CREATE TABLE api_calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT REFERENCES runs(run_id) ON DELETE SET NULL,
    endpoint        TEXT NOT NULL,        -- 'documents.json' | 'documents'
    params_masked   TEXT NOT NULL,
    http_status     INTEGER,
    content_type    TEXT,
    api_status      TEXT,                 -- in-body JSON status, if any
    bytes           INTEGER,
    elapsed_ms      INTEGER,
    at              TEXT NOT NULL
);
CREATE INDEX api_calls_run ON api_calls(run_id);

-- ---------------------------------------------------------------- entities
CREATE TABLE edinet_codes (
    edinet_code     TEXT PRIMARY KEY,
    sec_code5       TEXT,
    name            TEXT,
    listed          INTEGER CHECK (listed IN (0, 1)),
    fetched_at      TEXT
);

CREATE TABLE code_aggregation (          -- ESE140190 集約一覧 (abolished -> continuing)
    old_code        TEXT PRIMARY KEY,
    new_code        TEXT NOT NULL,
    source_date     TEXT
);

CREATE TABLE successor_map (             -- config/successor_map.csv (manual)
    prev_code       TEXT NOT NULL,
    next_code       TEXT NOT NULL,
    reason          TEXT,
    registered_by   TEXT,
    registered_at   TEXT,
    PRIMARY KEY (prev_code, next_code)
);

-- ---------------------------------------------------------------- document list
-- Append-only copy of every doclist record (never overwritten by re-scans; §6-1, §6-5).
CREATE TABLE doclist_raw (
    file_date       TEXT NOT NULL,        -- the date= parameter of documents.json
    seq_number      INTEGER NOT NULL,
    fetched_at      TEXT NOT NULL,
    doc_id          TEXT NOT NULL,
    ope_date_time   TEXT,
    record_json     TEXT NOT NULL,
    PRIMARY KEY (file_date, seq_number, fetched_at)
);
CREATE INDEX doclist_raw_doc ON doclist_raw(doc_id, ope_date_time);

-- Latest merged state per docID (max opeDateTime, then seqNumber).
CREATE TABLE documents (
    doc_id                  TEXT PRIMARY KEY,
    edinet_code             TEXT,
    entity_id               TEXT,         -- resolved by resolve.entity (aggregation + successor map)
    sec_code                TEXT,
    filer_name              TEXT,
    doc_description         TEXT,
    doc_type_code           TEXT,
    ordinance_code          TEXT,
    form_code               TEXT,
    period_start            TEXT,
    period_end              TEXT,
    submit_dt               TEXT,
    parent_doc_id           TEXT,
    xbrl_flag               TEXT,
    csv_flag                TEXT,
    withdrawal_status       TEXT,
    disclosure_status       TEXT,
    legal_status            TEXT,
    doc_info_edit_status    TEXT,
    last_ope_dt             TEXT,
    last_seq_number         INTEGER,
    last_seen_file_date     TEXT
);
CREATE INDEX documents_entity ON documents(entity_id, period_end);
CREATE INDEX documents_edinet ON documents(edinet_code, period_end);

-- ---------------------------------------------------------------- fetch state / retry queue
CREATE TABLE fetches (
    doc_id          TEXT NOT NULL REFERENCES documents(doc_id),
    type            INTEGER NOT NULL,     -- 1 = XBRL zip (the source of truth, §6-2)
    status          TEXT NOT NULL CHECK (status IN ('ok', 'failed', 'pending')),
    path            TEXT,                 -- relative to data_dir, raw/{docID[0:4]}/{docID}/type1.zip
    sha256          TEXT,
    size            INTEGER,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,                 -- key-masked message
    fetched_at      TEXT,
    PRIMARY KEY (doc_id, type)
);

-- ---------------------------------------------------------------- version / period resolution
CREATE TABLE filing_selection (
    run_id              TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    entity_id           TEXT NOT NULL,
    role                TEXT NOT NULL CHECK (role IN ('cur', 'prev')),
    doc_id              TEXT REFERENCES documents(doc_id),   -- NULL when there is no usable filing
    original_doc_id     TEXT REFERENCES documents(doc_id),
    period_end          TEXT,
    version_kind        TEXT,             -- original | correction | xbrl_fix ...
    flags_json          TEXT NOT NULL DEFAULT '[]',          -- irregular, prev_gap, correction_unparsed, ...
    no_prev_reason      TEXT,             -- new_listing | successor_suspected | prev_missing_year | ...
    reason_basis_json   TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (run_id, entity_id, role)
);

-- ---------------------------------------------------------------- derived (offline) stages
CREATE TABLE extracts (
    doc_id          TEXT NOT NULL REFERENCES documents(doc_id),
    extractor_ver   TEXT NOT NULL,
    input_sha256    TEXT NOT NULL,        -- sha256 of the raw ZIP
    source          TEXT NOT NULL CHECK (source IN ('xbrl', 'ixbrl')),
    raw_html_sha    TEXT,
    text            TEXT NOT NULL,        -- stored as-is; normalisation happens only when comparing
    char_len        INTEGER NOT NULL,
    img_count       INTEGER NOT NULL DEFAULT 0,
    hit_count       INTEGER NOT NULL DEFAULT 0,
    warnings_json   TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (doc_id, extractor_ver)
);

CREATE TABLE items (
    doc_id          TEXT NOT NULL REFERENCES documents(doc_id),
    segmenter_ver   TEXT NOT NULL,
    item_idx        INTEGER NOT NULL,
    input_sha256    TEXT NOT NULL,        -- sha256 of extracts.text
    level           INTEGER NOT NULL,
    heading_raw     TEXT,
    heading_norm    TEXT,
    body            TEXT NOT NULL,
    start_off       INTEGER NOT NULL,
    end_off         INTEGER NOT NULL,
    segmentation    TEXT NOT NULL CHECK (segmentation IN ('normal', 'fallback')),
    PRIMARY KEY (doc_id, segmenter_ver, item_idx),
    CHECK (start_off <= end_off)
);

-- One row per pair; 1:n pairs list several item_idx values in cur_items / prev_items (JSON arrays).
CREATE TABLE matches (
    cur_doc         TEXT NOT NULL REFERENCES documents(doc_id),
    prev_doc        TEXT NOT NULL REFERENCES documents(doc_id),
    matcher_ver     TEXT NOT NULL,
    pair_idx        INTEGER NOT NULL,
    input_sha256    TEXT NOT NULL,        -- hash of both item lists + thresholds fingerprint
    cur_items       TEXT NOT NULL DEFAULT '[]',
    prev_items      TEXT NOT NULL DEFAULT '[]',
    label           TEXT NOT NULL CHECK (label IN
                    ('same', 'modified', 'retitled', 'moved', 'merged', 'split', 'added', 'deleted')),
    stage           TEXT CHECK (stage IN ('anchor', 'assign', 'contain')),  -- NULL for added/deleted
    score_s         REAL,
    body_sim        REAL,
    head_sim        REAL,
    review_flag     INTEGER NOT NULL DEFAULT 0 CHECK (review_flag IN (0, 1)),
    renumbered      INTEGER NOT NULL DEFAULT 0 CHECK (renumbered IN (0, 1)),
    PRIMARY KEY (cur_doc, prev_doc, matcher_ver, pair_idx)
);

-- Sentence-level changes for the LLM (§5, §6-7). evidence_id is independent of segmentation.
CREATE TABLE sentence_changes (
    change_id         TEXT PRIMARY KEY,
    cur_doc           TEXT NOT NULL REFERENCES documents(doc_id),
    prev_doc          TEXT NOT NULL REFERENCES documents(doc_id),
    differ_ver        TEXT NOT NULL,
    pair_idx          INTEGER NOT NULL,
    op                TEXT NOT NULL CHECK (op IN ('add', 'del', 'mod')),
    cur_evidence_id   TEXT,
    prev_evidence_id  TEXT,
    text_cur          TEXT,
    text_prev         TEXT,
    offsets_json      TEXT NOT NULL DEFAULT '{}',
    is_minor          INTEGER NOT NULL DEFAULT 0 CHECK (is_minor IN (0, 1)),
    context_json      TEXT NOT NULL DEFAULT '{}',
    CHECK ((op = 'add' AND cur_evidence_id IS NOT NULL AND prev_evidence_id IS NULL)
        OR (op = 'del' AND cur_evidence_id IS NULL AND prev_evidence_id IS NOT NULL)
        OR (op = 'mod' AND cur_evidence_id IS NOT NULL AND prev_evidence_id IS NOT NULL))
);
CREATE INDEX sentence_changes_pair ON sentence_changes(cur_doc, prev_doc, differ_ver, pair_idx);

-- ---------------------------------------------------------------- monitoring (§8-3)
CREATE TABLE metrics (
    run_id          TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    metric          TEXT NOT NULL,
    entity_id       TEXT NOT NULL DEFAULT '',   -- '' = run-wide
    value           REAL,
    threshold       REAL,
    breached        INTEGER NOT NULL DEFAULT 0 CHECK (breached IN (0, 1)),
    PRIMARY KEY (run_id, metric, entity_id)
);
