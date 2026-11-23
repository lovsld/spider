CREATE TABLE IF NOT EXISTS crawl_snapshot (
    id            bigserial PRIMARY KEY,
    credit_code   varchar(32) NOT NULL,
    crawled_at    timestamptz NOT NULL,
    detail_url    text,
    payload       jsonb NOT NULL,
    imported_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (credit_code, crawled_at)
);

CREATE TABLE IF NOT EXISTS company (
    id                 bigserial PRIMARY KEY,
    credit_code        varchar(32) NOT NULL,
    name               text NOT NULL,
    legal_person       text,
    registration_type  text,
    region             text,
    address            text,
    detail_url         text,
    content_hash       text NOT NULL,
    valid_from         timestamptz NOT NULL,
    valid_to           timestamptz,
    is_current         boolean NOT NULL DEFAULT true,
    first_seen_at      timestamptz NOT NULL,
    last_seen_at       timestamptz NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS company_one_current
    ON company (credit_code) WHERE is_current;

CREATE TABLE IF NOT EXISTS qualification (
    id              bigserial PRIMARY KEY,
    credit_code     varchar(32) NOT NULL,
    business_key    text NOT NULL,
    category        text,
    cert_no         text,
    name            text,
    issued_on       text,
    expires_on      text,
    issuer          text,
    content_hash    text NOT NULL,
    payload         jsonb NOT NULL,
    valid_from      timestamptz NOT NULL,
    valid_to        timestamptz,
    is_current      boolean NOT NULL DEFAULT true,
    first_seen_at   timestamptz NOT NULL,
    last_seen_at    timestamptz NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS qualification_one_current
    ON qualification (credit_code, business_key) WHERE is_current;

CREATE TABLE IF NOT EXISTS registered_person (
    id               bigserial PRIMARY KEY,
    credit_code      varchar(32) NOT NULL,
    business_key     text NOT NULL,
    person_name      text,
    id_number        text,
    category         text,
    registration_no  text,
    specialty        text,
    content_hash     text NOT NULL,
    payload          jsonb NOT NULL,
    valid_from       timestamptz NOT NULL,
    valid_to         timestamptz,
    is_current       boolean NOT NULL DEFAULT true,
    first_seen_at    timestamptz NOT NULL,
    last_seen_at     timestamptz NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS registered_person_one_current
    ON registered_person (credit_code, business_key) WHERE is_current;

CREATE TABLE IF NOT EXISTS project (
    id             bigserial PRIMARY KEY,
    credit_code    varchar(32) NOT NULL,
    business_key   text NOT NULL,
    project_code   text,
    project_name   text,
    region         text,
    category       text,
    owner          text,
    content_hash   text NOT NULL,
    payload        jsonb NOT NULL,
    valid_from     timestamptz NOT NULL,
    valid_to       timestamptz,
    is_current     boolean NOT NULL DEFAULT true,
    first_seen_at  timestamptz NOT NULL,
    last_seen_at   timestamptz NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS project_one_current
    ON project (credit_code, business_key) WHERE is_current;

CREATE TABLE IF NOT EXISTS performance (
    id               bigserial PRIMARY KEY,
    credit_code      varchar(32) NOT NULL,
    business_key     text NOT NULL,
    data_level       text,
    project_name     text,
    record_no        text,
    project_code     text,
    performance_type text,
    detail_text      text,
    content_hash     text NOT NULL,
    payload          jsonb NOT NULL,
    valid_from       timestamptz NOT NULL,
    valid_to         timestamptz,
    is_current       boolean NOT NULL DEFAULT true,
    first_seen_at    timestamptz NOT NULL,
    last_seen_at     timestamptz NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS performance_one_current
    ON performance (credit_code, business_key) WHERE is_current;

CREATE TABLE IF NOT EXISTS misconduct (
    id             bigserial PRIMARY KEY,
    credit_code    varchar(32) NOT NULL,
    business_key   text NOT NULL,
    subject_no     text,
    decision       text,
    department     text,
    valid_period   text,
    content_hash   text NOT NULL,
    payload        jsonb NOT NULL,
    valid_from     timestamptz NOT NULL,
    valid_to       timestamptz,
    is_current     boolean NOT NULL DEFAULT true,
    first_seen_at  timestamptz NOT NULL,
    last_seen_at   timestamptz NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS misconduct_one_current
    ON misconduct (credit_code, business_key) WHERE is_current;

CREATE TABLE IF NOT EXISTS blacklist (
    id             bigserial PRIMARY KEY,
    credit_code    varchar(32) NOT NULL,
    business_key   text NOT NULL,
    subject_no     text,
    basis          text,
    department     text,
    valid_period   text,
    content_hash   text NOT NULL,
    payload        jsonb NOT NULL,
    valid_from     timestamptz NOT NULL,
    valid_to       timestamptz,
    is_current     boolean NOT NULL DEFAULT true,
    first_seen_at  timestamptz NOT NULL,
    last_seen_at   timestamptz NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS blacklist_one_current
    ON blacklist (credit_code, business_key) WHERE is_current;

CREATE TABLE IF NOT EXISTS punishment (
    id             bigserial PRIMARY KEY,
    credit_code    varchar(32) NOT NULL,
    business_key   text NOT NULL,
    record_no      text,
    subject        text,
    legal_person   text,
    reason         text,
    department     text,
    listed_on      text,
    content_hash   text NOT NULL,
    payload        jsonb NOT NULL,
    valid_from     timestamptz NOT NULL,
    valid_to       timestamptz,
    is_current     boolean NOT NULL DEFAULT true,
    first_seen_at  timestamptz NOT NULL,
    last_seen_at   timestamptz NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS punishment_one_current
    ON punishment (credit_code, business_key) WHERE is_current;

CREATE TABLE IF NOT EXISTS change_log (
    id             bigserial PRIMARY KEY,
    credit_code    varchar(32) NOT NULL,
    business_key   text NOT NULL,
    content_hash   text NOT NULL,
    payload        jsonb NOT NULL,
    valid_from     timestamptz NOT NULL,
    valid_to       timestamptz,
    is_current     boolean NOT NULL DEFAULT true,
    first_seen_at  timestamptz NOT NULL,
    last_seen_at   timestamptz NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS change_log_one_current
    ON change_log (credit_code, business_key) WHERE is_current;

CREATE TABLE IF NOT EXISTS schema_meta (
    version       int NOT NULL,
    applied_at    timestamptz NOT NULL DEFAULT now()
);

-- === Leads / CRM (retrieval system) =====================================

CREATE TABLE IF NOT EXISTS app_user (
    id            bigserial PRIMARY KEY,
    username      varchar(64) NOT NULL UNIQUE,
    password_hash text NOT NULL,
    display_name  text,
    role          varchar(16) NOT NULL DEFAULT 'member',
    active        boolean NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS app_session (
    token      varchar(64) PRIMARY KEY,
    user_id    bigint NOT NULL,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS lead (
    id                 bigserial PRIMARY KEY,
    credit_code        varchar(32) NOT NULL,
    business_key       text NOT NULL,
    qualification_name text,
    category           text,
    cert_no            text,
    expires_on         date,
    urgency            varchar(16) NOT NULL,
    status             varchar(16) NOT NULL DEFAULT 'pool',
    close_reason       text,
    owner_id           bigint,
    claimed_at         timestamptz,
    last_contact_at    timestamptz,
    next_follow_at     timestamptz,
    cooldown_until     timestamptz,
    no_answer_count    int NOT NULL DEFAULT 0,
    prior_owner_id     bigint,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS lead_one_active
    ON lead (credit_code, business_key) WHERE status IN ('pool', 'claimed', 'following');

CREATE TABLE IF NOT EXISTS contact_record (
    id             bigserial PRIMARY KEY,
    lead_id        bigint NOT NULL,
    credit_code    varchar(32) NOT NULL,
    user_id        bigint NOT NULL,
    outcome        varchar(24) NOT NULL,
    note           text,
    product        text,
    amount         numeric,
    next_follow_at timestamptz,
    phone_used     text,
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS company_phone (
    id         bigserial PRIMARY KEY,
    credit_code varchar(32) NOT NULL,
    phone      varchar(32) NOT NULL,
    kind       varchar(16),
    source     varchar(24) NOT NULL,
    validity   varchar(16) NOT NULL DEFAULT 'unknown',
    marked_by  bigint,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (credit_code, phone)
);

CREATE TABLE IF NOT EXISTS do_not_call (
    credit_code varchar(32) PRIMARY KEY,
    reason      text,
    created_by  bigint,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS system_config (
    key        varchar(64) PRIMARY KEY,
    value      text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
