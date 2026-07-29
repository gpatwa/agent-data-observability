-- Wide-schema condition: the same orders/refunds data, buried in a realistic
-- warehouse of ~120 tables. The agent is told nothing about the schema and must
-- discover which tables matter before it can answer anything.
--
-- This tests a different redundancy mechanism from the others: the BAIR post
-- describes agents "forming probing queries to test which data sources yield
-- meaningful signals". Two clean tables cannot produce that behaviour. This can.

DO $$
DECLARE
  -- Plausible decoys. Several carry amount/date columns so they look relevant
  -- to a revenue question and cannot be dismissed from the name alone.
  names text[] := ARRAY[
    'web_sessions','page_views','ad_impressions','ad_clicks','ad_spend_daily',
    'campaigns','campaign_budgets','campaign_targets','attribution_touchpoints',
    'customers','customer_segments','customer_addresses','customer_consents',
    'subscriptions','subscription_events','plans','plan_prices','discounts',
    'coupons','coupon_redemptions','gift_cards','loyalty_points','referrals',
    'invoices','invoice_lines','payments','payment_methods','payment_attempts',
    'chargebacks','settlements','fx_rates','tax_rates','tax_filings',
    'shipments','shipment_events','carriers','delivery_slas','returns_rma',
    'inventory_snapshots','inventory_movements','warehouse_locations','bins',
    'products','product_variants','product_prices','price_changes','categories',
    'category_hierarchy','brands','suppliers','supplier_contracts',
    'purchase_orders','purchase_order_lines','goods_receipts','stock_counts',
    'support_tickets','ticket_messages','ticket_sla_breaches','agents_roster',
    'nps_responses','csat_responses','survey_questions','survey_answers',
    'email_sends','email_opens','email_clicks','email_bounces','unsubscribes',
    'push_notifications','sms_sends','in_app_messages','app_events',
    'feature_flags','flag_exposures','experiments','experiment_assignments',
    'experiment_metrics','cohorts','cohort_members','churn_predictions',
    'ltv_estimates','forecast_runs','forecast_outputs','budget_versions',
    'gl_accounts','journal_entries','cost_centers','allocations','accruals',
    'headcount','payroll_runs','vendor_invoices','contracts','renewals',
    'partners','partner_payouts','affiliate_clicks','affiliate_conversions',
    'stores','store_hours','regions_dim','channels_dim','currencies_dim',
    'calendar_dim','fiscal_periods','data_quality_checks','pipeline_runs',
    'audit_log','access_grants','api_keys_meta','rate_limit_events',
    'search_queries','search_impressions','recommendations','wishlists',
    'carts','cart_items','checkout_steps','abandoned_carts','sessions_replay'
  ];
  n text;
BEGIN
  FOREACH n IN ARRAY names LOOP
    EXECUTE format('DROP TABLE IF EXISTS %I CASCADE', n);
    EXECUTE format(
      'CREATE TABLE %I (
         id bigserial PRIMARY KEY,
         entity_id int,
         event_date date,
         amount numeric(10,2),
         status text,
         note text
       )', n);
    -- A little data in each, so "is this table empty?" is not a free filter.
    EXECUTE format(
      'INSERT INTO %I (entity_id, event_date, amount, status, note)
       SELECT (random()*5000)::int,
              (DATE ''2026-06-01'' + (random()*60)::int)::date,
              (random()*500)::numeric(10,2),
              (ARRAY[''ok'',''pending'',''failed''])[1+floor(random()*3)],
              %L
       FROM generate_series(1, 200)', n, 'synthetic decoy row');
  END LOOP;
END $$;

ANALYZE;
