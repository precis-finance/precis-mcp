-- Intercompany recharge view — intra-group cross-charges (actuals only).
-- Joins the recharge fact to the cost-centre master for both the charged and
-- the counterparty cost centre. Not reconciled to the GL.

-- Hierarchy parents (department/division off both the charged and counterparty
-- cost centre) are resolved by the engine via a leaf-dimension join at query
-- time, so they are not denormalised here. Period parents stay.
WITH cc_dim AS (
    SELECT cost_centre, cost_centre_name
    FROM live.dim_cost_centre
),
period_dim AS (
    SELECT period, quarter, fiscal_year
    FROM live.dim_period
)

SELECT
    concat(f.period, f.cost_centre, f.counterparty_cc) AS pk,
    'ENT-001' AS entity_id,
    f.cost_centre AS cost_centre,
    coalesce(cc.cost_centre_name, '') AS cost_centre_name,
    f.counterparty_cc AS counterparty_cc,
    coalesce(cp.cost_centre_name, '') AS counterparty_cc_name,
    f.period AS period,
    coalesce(pd.quarter, '')     AS quarter,
    coalesce(pd.fiscal_year, '') AS fiscal_year,
    'ACTUALS' AS scenario,
    '__actuals__' AS commit_id,
    SUM(f.amount) AS amount
FROM live.fact_intercompany f
LEFT JOIN cc_dim cc     ON f.cost_centre = cc.cost_centre
LEFT JOIN cc_dim cp     ON f.counterparty_cc = cp.cost_centre
LEFT JOIN period_dim pd ON f.period = pd.period
GROUP BY
    f.period, f.cost_centre, cc.cost_centre_name,
    f.counterparty_cc, cp.cost_centre_name,
    pd.quarter, pd.fiscal_year
