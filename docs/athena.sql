-- Firehose writes gzipped JSON lines to s3://<DataLakeBucket>/decisions/dt=YYYY-MM-DD/
-- Replace <DataLakeBucket> with the value from the CDK output.

CREATE EXTERNAL TABLE IF NOT EXISTS fraud_decisions (
  txn_id string, user_id string, ts double, amount double, city string, country string,
  lat double, lon double, merchant_category string, risk_score double, model_score double,
  rule_score double, rules_fired array<string>, decision string, model_version string,
  latency_ms double, label int, fraud_type string,
  f_velocity_1m double, f_velocity_10m double, f_amount_ratio double, f_amount_zscore double,
  f_speed_kmh double, f_km_from_last double, f_is_new_device double, f_is_new_country double
)
PARTITIONED BY (dt string)
ROW FORMAT SERDE 'org.openx.data.jsonserde.JsonSerDe'
LOCATION 's3://<DataLakeBucket>/decisions/'
TBLPROPERTIES (
  'projection.enabled' = 'true',
  'projection.dt.type' = 'date',
  'projection.dt.format' = 'yyyy-MM-dd',
  'projection.dt.range' = '2025-01-01,NOW',
  'storage.location.template' = 's3://<DataLakeBucket>/decisions/dt=${dt}'
);

-- Decision mix per day
SELECT dt, decision, count(*) AS n, round(sum(amount), 2) AS dollars
FROM fraud_decisions GROUP BY dt, decision ORDER BY dt, decision;

-- Live precision / recall against simulator ground truth
SELECT
  sum(CASE WHEN decision <> 'ALLOW' AND label = 1 THEN 1 ELSE 0 END) * 1.0
    / nullif(sum(CASE WHEN decision <> 'ALLOW' THEN 1 ELSE 0 END), 0) AS precision,
  sum(CASE WHEN decision <> 'ALLOW' AND label = 1 THEN 1 ELSE 0 END) * 1.0
    / nullif(sum(label), 0) AS recall
FROM fraud_decisions WHERE dt = cast(current_date AS varchar);

-- Which rules fire most, and how often they are right
SELECT rule, count(*) AS fired, avg(label) AS precision
FROM fraud_decisions CROSS JOIN UNNEST(rules_fired) AS t(rule)
GROUP BY rule ORDER BY fired DESC;

-- Scoring latency percentiles by model version
SELECT model_version, approx_percentile(latency_ms, 0.5) AS p50_ms,
       approx_percentile(latency_ms, 0.99) AS p99_ms
FROM fraud_decisions GROUP BY model_version;
