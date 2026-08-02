#!/usr/bin/env bash
# Human/pipeline baseline from Redset — Amazon's published trace of real
# Amazon Redshift production fleets.
#
# WHY THIS EXISTS: every redundancy number in this repo was measured on agent
# traffic, with no baseline for what NON-agent warehouse traffic looks like on
# the same metric. Without that, "agents show 9-43% redundancy" is unanchored.
#
# Redset carries no SQL text, so the shape/subsumption analysis cannot run on
# it. It does carry `feature_fingerprint` — "a proxy for query-likeness, not
# based on text" — which is the closest available analogue to this repo's
# ast_hash tier. Redundancy is computed here with THIS repo's definition,
# 1 - distinct/total, so the comparison is like-for-like as far as it can be.
#
# Data: Redset (c) 2024 Amazon, CC BY-NC 4.0.
#   https://github.com/amazon-science/redset
#   Why TPC Is Not Enough: An Analysis of the Amazon Redshift Fleet, VLDB 2024
# NON-COMMERCIAL USE ONLY — note this before reusing these numbers commercially.
#
# Usage: ./scripts/redset-baseline.sh [num_parts]   (default 20)
set -euo pipefail

command -v duckdb >/dev/null || { echo "error: duckdb not on PATH" >&2; exit 1; }

PARTS="${1:-20}"
BASE="https://redshift-downloads.s3.amazonaws.com/redset/provisioned/parts"
URLS=$(for i in $(seq 0 $((PARTS - 1))); do printf "'%s/%s.parquet'," "$BASE" "$i"; done | sed 's/,$//')

echo "==> reading $PARTS Redset parts (streamed from S3, nothing stored locally)"

duckdb -c "
INSTALL httpfs; LOAD httpfs;

CREATE OR REPLACE TABLE r AS
SELECT instance_id, feature_fingerprint
FROM read_parquet([$URLS])
WHERE lower(query_type) = 'select'          -- comparable to this repo's read-only workload
  AND feature_fingerprint IS NOT NULL;

SELECT count(*) AS select_queries,
       count(DISTINCT instance_id) AS clusters
FROM r;

CREATE OR REPLACE TABLE per_cluster AS
SELECT instance_id,
       count(*) AS queries,
       count(DISTINCT feature_fingerprint) AS distinct_fingerprints,
       1.0 - (count(DISTINCT feature_fingerprint)::DOUBLE / count(*)) AS redundancy
FROM r
GROUP BY 1
HAVING count(*) >= 100;                     -- ignore near-idle clusters

SELECT count(*)                                                  AS clusters_scored,
       round(100 * median(redundancy), 1)                        AS median_pct,
       round(100 * quantile(redundancy, 0.25), 1)                AS p25_pct,
       round(100 * quantile(redundancy, 0.75), 1)                AS p75_pct,
       round(100 * min(redundancy), 1)                           AS min_pct,
       round(100 * max(redundancy), 1)                           AS max_pct,
       round(100 * avg(CASE WHEN redundancy >= 0.8 THEN 1 ELSE 0 END), 1) AS pct_clusters_over_80
FROM per_cluster;
"
