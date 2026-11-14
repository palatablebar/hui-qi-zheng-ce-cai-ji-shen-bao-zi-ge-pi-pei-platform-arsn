-- 政策组 / 名额占位（PostgreSQL 语法，运行时不执行；建表与补列由应用启动时自动完成）
--
-- 语义：
--   policy_groups        互斥政策组 + 组级名额（正式 / 候补）+ 公示状态
--   allocations          名额占位台账：同组同政策同企业唯一；撤销正式占位由同组下一位候补递补
--   policies.group_id    政策所属组（NULL = 不分组，不参与名额分配）
--   match_runs.batch_key 批次键（政策窗口哈希）：重跑同一批直接复用既定结果

CREATE TABLE policy_groups (
    id              SERIAL PRIMARY KEY,
    code            VARCHAR(40)  NOT NULL UNIQUE,
    name            VARCHAR(120) NOT NULL,
    quota_official  INTEGER      NOT NULL DEFAULT 0,
    quota_waitlist  INTEGER      NOT NULL DEFAULT 0,
    public_status   VARCHAR(16)  NOT NULL DEFAULT 'unpublished',
    note            TEXT         NOT NULL DEFAULT '',
    seed_key        VARCHAR(120) UNIQUE,
    created_at      TIMESTAMP    NOT NULL,
    updated_at      TIMESTAMP    NOT NULL
);

CREATE TABLE allocations (
    id               SERIAL PRIMARY KEY,
    group_id         INTEGER NOT NULL REFERENCES policy_groups(id) ON DELETE CASCADE,
    policy_id        INTEGER NOT NULL REFERENCES policies(id) ON DELETE CASCADE,
    company_id       INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    run_id           INTEGER NOT NULL REFERENCES match_runs(id) ON DELETE CASCADE,
    match_id         INTEGER REFERENCES matches(id) ON DELETE SET NULL,
    batch_key        VARCHAR(60) NOT NULL DEFAULT '',
    status           VARCHAR(16) NOT NULL DEFAULT 'waitlist',
    reason           VARCHAR(16),
    rank             INTEGER,
    promoted_from_id INTEGER REFERENCES allocations(id) ON DELETE SET NULL,
    note             TEXT,
    created_at       TIMESTAMP NOT NULL,
    updated_at       TIMESTAMP NOT NULL,
    revoked_at       TIMESTAMP,
    promoted_at      TIMESTAMP,
    CONSTRAINT uq_allocations_group_pair UNIQUE (group_id, policy_id, company_id)
);

ALTER TABLE policies ADD COLUMN group_id INTEGER REFERENCES policy_groups(id) ON DELETE SET NULL;
CREATE INDEX ix_policies_group_id ON policies (group_id);

ALTER TABLE match_runs ADD COLUMN batch_key VARCHAR(60);
CREATE UNIQUE INDEX uq_match_runs_batch_key ON match_runs (batch_key);
