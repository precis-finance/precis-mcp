-- Revenue subledger fact view: project × period grain.
-- Source: live.fact_revenue_subledger.
--
-- Memo subledger to the GL: reconciles 1:1 against GL revenue at the
-- project×period grain on the revenue side, and against GL direct cost
-- at the CC×period grain on the cost side (subledger captures only
-- billable project effort, never bench / indirect / SG&A).
--
-- Stock metrics (cum_*, wip_balance, percent_complete) are already
-- cumulative at row level — engine aggregates them with rollup_method=closing
-- across periods.
--
-- Landing-shape notes vs. the prior gl.revenue_subledger (now live.fact_revenue_subledger):
--   - Column names strip the `_eur` suffix; renamed back at the view boundary.
--   - `margin_recognised` derived as (revenue - cost). Landing does not
--     carry it as a stored column.
--   - `etc_cost_eur` and `eac_cost_eur` are not landed — no catalogue metric
--     references them. Retained here as NULL placeholders.

-- Cost-centre hierarchy parents (department/division) are resolved by the
-- engine via a leaf-dimension join at query time, so they are not denormalised
-- here. Period parents (quarter/fiscal_year) and client attributes stay.
SELECT
    s.project_id     AS project,
    s.cost_centre AS cost_centre,
    s.client_id                  AS client,
    coalesce(cl.client_name, '') AS client_name,
    coalesce(cl.industry, '')    AS client_industry,
    coalesce(cl.tier, '')        AS client_tier,
    s.project_type,
    s.recognition_method,
    formatDateTime(s.recognition_date, '%Y-%m') AS period,
    coalesce(pd.quarter, '')     AS quarter,
    coalesce(pd.fiscal_year, '') AS fiscal_year,
    'ACTUALS'                    AS scenario,
    s.currency,
    -- effort
    s.hours_worked,
    s.hours_billable,
    -- period flow
    s.revenue_recognised                         AS revenue_recognised,
    s.cost_recognised                            AS cost_recognised,
    (s.revenue_recognised - s.cost_recognised)   AS margin_recognised,
    s.amount_billed                              AS amount_billed,
    -- contract / progress
    s.contract_value                             AS contract_value,
    CAST(NULL AS Nullable(Decimal(14,2)))        AS etc_cost,
    CAST(NULL AS Nullable(Decimal(14,2)))        AS eac_cost,
    s.percent_complete,
    -- cumulative stock (period-end)
    s.cum_revenue_recognised                     AS cum_revenue_recognised,
    s.cum_cost_recognised                        AS cum_cost_recognised,
    s.cum_billed                                 AS cum_billed,
    s.wip_balance                                AS wip_balance
FROM live.fact_revenue_subledger s
LEFT JOIN live.dim_client cl
    ON s.client_id = cl.client_id
LEFT JOIN live.dim_period pd
    ON formatDateTime(s.recognition_date, '%Y-%m') = pd.period
