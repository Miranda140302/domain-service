CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE IF NOT EXISTS proxy_requests (
    timestamp TIMESTAMPTZ NOT NULL,
    domain TEXT NOT NULL,
    client_ip INET,
    method TEXT NOT NULL,
    status SMALLINT NOT NULL
);

SELECT create_hypertable(
    'proxy_requests',
    'timestamp',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS proxy_requests_domain_timestamp_idx
    ON proxy_requests (domain, timestamp DESC);
