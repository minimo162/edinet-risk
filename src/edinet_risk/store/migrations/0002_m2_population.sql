-- edinet-risk SQLite schema, version 2 (plan §5; M2 batch 1).
-- Applied by store/migrate.py inside one transaction; user_version is set there.
--
-- Checked against plan §5: the only M2 table missing from v1 is population; every column §5 lists
-- for the v1 tables is already present, so no ALTER TABLE is needed here.
-- llm_cache / llm_results stay deferred to M3.

-- ---------------------------------------------------------------- population (JPX list x EDINET codes)
-- One row per JPX listing at the base month (`population load --as-of-month`, §3-2).
--   * sec_code4: JPX 4-character code, always TEXT. Codes listed since 2024 may contain letters
--     (e.g. '130A'); stored upper-case. The EDINET secCode is sec_code4 || '0' (5 characters).
--   * edinet_code: NULL when the join failed; "コード不一致" when NULL or absent from edinet_codes
--     (§6-5), hence no foreign key to edinet_codes.
--   * match_method: how edinet_code was found (set by population.load).
CREATE TABLE population (
    as_of_month     TEXT NOT NULL CHECK (as_of_month GLOB '[0-9][0-9][0-9][0-9]-[01][0-9]'),
    sec_code4       TEXT NOT NULL CHECK (length(sec_code4) = 4
                                         AND sec_code4 GLOB '[0-9][0-9A-Z][0-9A-Z][0-9A-Z]'),
    name            TEXT,
    market          TEXT,
    sector33        TEXT,
    edinet_code     TEXT,
    match_method    TEXT,
    PRIMARY KEY (as_of_month, sec_code4)
);
CREATE INDEX population_edinet ON population(edinet_code);
