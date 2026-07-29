DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS refunds;

CREATE TABLE orders (
  order_id    bigserial PRIMARY KEY,
  order_date  date NOT NULL,
  region      text NOT NULL,
  channel     text NOT NULL,
  customer_id int  NOT NULL,
  amount      numeric(10,2) NOT NULL
);

CREATE TABLE refunds (
  refund_id  bigserial PRIMARY KEY,
  order_id   bigint NOT NULL,
  refund_date date NOT NULL,
  amount     numeric(10,2) NOT NULL
);

-- ~120k orders across Jun+Jul 2026. July revenue drops because the EMEA
-- paid-search channel collapses partway through the month.
INSERT INTO orders (order_date, region, channel, customer_id, amount)
SELECT
  d::date,
  r,
  c,
  (random() * 20000)::int,
  CASE
    WHEN r = 'EMEA' AND c = 'paid_search' AND d >= DATE '2026-07-12'
      THEN (random() * 30 + 10)::numeric(10,2)
    ELSE (random() * 220 + 40)::numeric(10,2)
  END
FROM generate_series(DATE '2026-06-01', DATE '2026-07-31', INTERVAL '1 day') d
CROSS JOIN unnest(ARRAY['AMER','EMEA','APAC']) r
CROSS JOIN unnest(ARRAY['paid_search','organic','partner','email']) c
CROSS JOIN generate_series(1, 6000) n;

INSERT INTO refunds (order_id, refund_date, amount)
SELECT order_id, order_date + 5, amount * 0.5
FROM orders
WHERE random() < 0.002;

CREATE INDEX ON orders (order_date);
CREATE INDEX ON orders (region, channel);
ANALYZE orders;
ANALYZE refunds;
