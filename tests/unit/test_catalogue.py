# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Tests for precis_mcp/engine/catalogue.py"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from precis_mcp.engine.catalogue import (
    BaseMetric,
    Catalogue,
    CatalogueError,
    CubeDimension,
    DerivedMetric,
    Dimension,
    DimensionAttribute,
    DimensionSource,
    DerivedFrom,
    MetricPredicate,
    ParentRelationship,
    RaggedLevel,
    RaggedSource,
    PlanDataset,
    PlanDatasetDimension,
    load_catalogue,
    resolve_statement,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CATALOGUE_DIR = PROJECT_ROOT / "instance" / "catalogue"


def write_yml(tmp_path: Path, filename: str, content: str) -> Path:
    """Write a YAML string to a temp file and return the dir."""
    p = tmp_path / filename
    p.write_text(textwrap.dedent(content))
    return tmp_path


# ---------------------------------------------------------------------------
# 1. Loading valid catalogue
# ---------------------------------------------------------------------------

class TestLoadValidCatalogue:
    def test_metrics_load(self):
        cat = load_catalogue(str(CATALOGUE_DIR))
        base = [m for m in cat.metrics.values() if isinstance(m, BaseMetric)]
        derived = [m for m in cat.metrics.values() if isinstance(m, DerivedMetric)]
        assert base
        assert derived
        assert "revenue" in cat.metrics
        assert "gross_margin" in cat.metrics

    def test_catalogue_no_longer_loads_scenarios(self):
        cat = load_catalogue(str(CATALOGUE_DIR))
        assert cat.scenarios == {}

    def test_statements_load(self):
        cat = load_catalogue(str(CATALOGUE_DIR))
        assert cat.statements
        assert "pnl" in cat.statements
        assert "full_pnl" in cat.statements

    def test_domain_loaded(self):
        cat = load_catalogue(str(CATALOGUE_DIR))
        assert "pnl" in cat.domains
        assert "gl" in cat.domains
        assert "timesheets" in cat.domains
        assert "payroll" in cat.domains


# ---------------------------------------------------------------------------
# 2. Base metric parsing
# ---------------------------------------------------------------------------

class TestBaseMetricParsing:
    def test_revenue_fields(self):
        cat = load_catalogue(str(CATALOGUE_DIR))
        revenue = cat.metrics["revenue"]
        assert isinstance(revenue, BaseMetric)
        assert revenue.label == "Revenue"
        assert revenue.where == [MetricPredicate(column="fs_line", op="eq", value="Revenue")]
        assert revenue.source_column == "amount"
        assert revenue.aggregation == "sum"
        assert revenue.rollup_method == "sum"
        assert revenue.sign == "abs"
        assert revenue.format == "currency"
        assert revenue.style == "header"
        assert revenue.indent == 0
        assert revenue.fs_group == "Revenue"

    def test_avg_fte_billable_rollup(self):
        cat = load_catalogue(str(CATALOGUE_DIR))
        m = cat.metrics["avg_fte_billable"]
        assert isinstance(m, BaseMetric)
        assert m.rollup_method == "avg"

    def test_closing_fte_rollup(self):
        cat = load_catalogue(str(CATALOGUE_DIR))
        m = cat.metrics["closing_fte_billable"]
        assert isinstance(m, BaseMetric)
        assert m.rollup_method == "closing"

    def test_where_metric_parses_predicates(self, tmp_path):
        write_yml(tmp_path, "test.yml", """
            domain: test
            source_view: semantic.test
            metrics:
              - key: filtered_amount
                label: Filtered amount
                source_column: amount
                aggregation: sum
                rollup_method: sum
                sign: raw
                format: currency
                fs_group: Test
                where:
                  - column: category
                    op: eq
                    value: AAA
                  - column: region
                    op: in
                    values: [UK, US]
        """)
        cat = load_catalogue(str(tmp_path))
        metric = cat.metrics["filtered_amount"]
        assert isinstance(metric, BaseMetric)
        assert metric.where == [
            MetricPredicate(column="category", op="eq", value="AAA"),
            MetricPredicate(column="region", op="in", values=["UK", "US"]),
        ]

    def test_source_filter_rejected(self, tmp_path):
        write_yml(tmp_path, "test.yml", """
            domain: test
            source_view: semantic.test
            metrics:
              - key: bad
                label: Bad
                source_filter: "category = 'AAA'"
                source_column: amount
                aggregation: sum
                rollup_method: sum
                sign: raw
                format: currency
                fs_group: Test
        """)
        with pytest.raises(CatalogueError, match="'source_filter' is no longer supported"):
            load_catalogue(str(tmp_path))

    def test_golden_federated_catalogue_loads(self):
        cat = load_catalogue(str(CATALOGUE_DIR))
        domain = cat.domains["gl_federated"]
        assert domain.backend_kind == "ibis"
        assert domain.backend == "customer_pg"
        metric = cat.metrics["federated_net_amount"]
        assert isinstance(metric, BaseMetric)
        assert metric.source_column == "amount"
        assert metric.where == []

    def test_federated_domain_rejects_non_sum_rollup(self, tmp_path):
        write_yml(tmp_path, "test.yml", """
            domain: fed
            source_view: finance.metric_view
            backend_kind: ibis
            backend: customer_pg
            versioned: false
            metrics:
              - key: bad
                label: Bad
                source_column: amount
                aggregation: sum
                rollup_method: avg
                sign: raw
                format: currency
                fs_group: Test
        """)
        with pytest.raises(CatalogueError, match="supports only 'sum'"):
            load_catalogue(str(tmp_path))

    def test_federated_domain_rejects_versioned_true(self, tmp_path):
        write_yml(tmp_path, "test.yml", """
            domain: fed
            source_view: finance.metric_view
            backend_kind: ibis
            backend: customer_pg
            versioned: true
            metrics:
              - key: amount
                label: Amount
                source_column: amount
                aggregation: sum
                rollup_method: sum
                sign: raw
                format: currency
                fs_group: Test
        """)
        with pytest.raises(CatalogueError, match="versioned=false"):
            load_catalogue(str(tmp_path))

    def test_clickhouse_domain_defaults_versioned_false(self, tmp_path):
        """versioned defaults to False — a ClickHouse domain that omits the flag
        is unversioned (no commit_id filter). Commit-aware domains must opt in."""
        write_yml(tmp_path, "test.yml", """
            domain: actuals
            source_view: semantic.v_actuals
            metrics:
              - key: amount
                label: Amount
                source_column: amount
                aggregation: sum
                rollup_method: sum
                sign: raw
                format: currency
                fs_group: Test
        """)
        cat = load_catalogue(str(tmp_path))
        assert cat.domains["actuals"].versioned is False

    def test_clickhouse_domain_honours_explicit_versioned_true(self, tmp_path):
        """A ClickHouse domain still opts into versioning explicitly."""
        write_yml(tmp_path, "test.yml", """
            domain: plan
            source_view: semantic.v_plan
            versioned: true
            metrics:
              - key: amount
                label: Amount
                source_column: amount
                aggregation: sum
                rollup_method: sum
                sign: raw
                format: currency
                fs_group: Test
        """)
        cat = load_catalogue(str(tmp_path))
        assert cat.domains["plan"].versioned is True


class TestInspectionCatalogue:
    def test_inspection_fields_load(self, tmp_path):
        write_yml(tmp_path, "test.yml", """
            domain: test
            source_view: semantic.test
            inspect_enabled: true
            inspect_columns:
              - period
              - account
              - amount
            metrics:
              - key: amount
                label: Amount
                source_column: amount
                aggregation: sum
                rollup_method: sum
                sign: raw
                format: currency
                fs_group: Test
        """)
        cat = load_catalogue(str(tmp_path))
        domain = cat.domains["test"]
        assert domain.inspect_enabled is True
        assert domain.inspect_columns == ["period", "account", "amount"]

    def test_inspection_enabled_requires_columns(self, tmp_path):
        write_yml(tmp_path, "test.yml", """
            domain: test
            source_view: semantic.test
            inspect_enabled: true
            metrics:
              - key: amount
                label: Amount
                source_column: amount
                aggregation: sum
                rollup_method: sum
                sign: raw
                format: currency
                fs_group: Test
        """)
        with pytest.raises(CatalogueError, match="inspect_columns is empty"):
            load_catalogue(str(tmp_path))

    def test_inspection_columns_reject_duplicates(self, tmp_path):
        write_yml(tmp_path, "test.yml", """
            domain: test
            source_view: semantic.test
            inspect_enabled: true
            inspect_columns: [period, period]
            metrics:
              - key: amount
                label: Amount
                source_column: amount
                aggregation: sum
                rollup_method: sum
                sign: raw
                format: currency
                fs_group: Test
        """)
        with pytest.raises(CatalogueError, match="duplicate inspect column"):
            load_catalogue(str(tmp_path))

    def test_inspection_columns_reject_unsafe_names(self, tmp_path):
        write_yml(tmp_path, "test.yml", """
            domain: test
            source_view: semantic.test
            inspect_enabled: true
            inspect_columns: [period, "bad-name"]
            metrics:
              - key: amount
                label: Amount
                source_column: amount
                aggregation: sum
                rollup_method: sum
                sign: raw
                format: currency
                fs_group: Test
        """)
        with pytest.raises(CatalogueError, match="invalid inspect column"):
            load_catalogue(str(tmp_path))


# ---------------------------------------------------------------------------
# 3. Derived metric parsing
# ---------------------------------------------------------------------------

class TestDerivedMetricParsing:
    def test_gross_margin_has_formula(self):
        cat = load_catalogue(str(CATALOGUE_DIR))
        gm = cat.metrics["gross_margin"]
        assert isinstance(gm, DerivedMetric)
        assert gm.formula == "revenue - direct_cost"
        assert gm.format == "currency"
        assert gm.style == "subtotal"
        assert gm.separator_above is True

    def test_derived_has_no_source_filter(self):
        cat = load_catalogue(str(CATALOGUE_DIR))
        gm = cat.metrics["gross_margin"]
        assert isinstance(gm, DerivedMetric)
        assert not hasattr(gm, "source_filter")

    def test_gross_margin_pct(self):
        cat = load_catalogue(str(CATALOGUE_DIR))
        gmp = cat.metrics["gross_margin_pct"]
        assert isinstance(gmp, DerivedMetric)
        assert gmp.format == "percent"
        assert gmp.style == "ratio"


# ---------------------------------------------------------------------------
# 5. Statement resolution
# ---------------------------------------------------------------------------

class TestStatementResolution:
    def test_statement_descriptions_loaded(self):
        cat = load_catalogue(str(CATALOGUE_DIR))
        assert cat.statements["pnl"].description != ""
        assert cat.statements["full_pnl"].description != ""
        assert "KPI" in cat.statements["full_pnl"].description

    def test_pnl_flat_list(self):
        cat = load_catalogue(str(CATALOGUE_DIR))
        result = resolve_statement(cat, "pnl")
        assert result[0] == "revenue"
        assert result[1] == "direct_cost"
        assert "gross_margin" in result
        assert "separator" in result
        assert "ebitda" in result
        # lines statement — no expansion, flat
        assert result.count("separator") == 2

    def test_full_pnl_expands_concat(self):
        cat = load_catalogue(str(CATALOGUE_DIR))
        result = resolve_statement(cat, "full_pnl")
        # Should contain entries from pnl, statistical, ratios + separators between parts
        assert "revenue" in result
        assert "billable_hours" in result
        assert "revenue_per_fte" in result
        # Separators between parts: the concat list has 2 'separator' entries
        # plus the separators within the pnl and statistical sub-statements
        assert result.count("separator") >= 2

    def test_full_pnl_preserves_order(self):
        cat = load_catalogue(str(CATALOGUE_DIR))
        result = resolve_statement(cat, "full_pnl")
        # revenue comes before billable_hours (pnl before statistical)
        assert result.index("revenue") < result.index("billable_hours")
        # billable_hours comes before revenue_per_fte (statistical before ratios)
        assert result.index("billable_hours") < result.index("revenue_per_fte")

    def test_unknown_statement_raises(self):
        cat = load_catalogue(str(CATALOGUE_DIR))
        with pytest.raises(CatalogueError, match="Unknown statement"):
            resolve_statement(cat, "nonexistent_statement")


# ---------------------------------------------------------------------------
# 7. Duplicate metric key detection
# ---------------------------------------------------------------------------

class TestDuplicateMetricKey:
    def test_duplicate_raises(self, tmp_path):
        write_yml(tmp_path, "dup.yml", """
            domain: test
            source_view: semantic.v_test
            metrics:
              - key: revenue
                label: Revenue
                source_column: amount
                aggregation: sum
                rollup_method: sum
                sign: abs
                format: currency
                fs_group: Revenue
              - key: revenue
                label: Revenue Duplicate
                source_column: amount
                aggregation: sum
                rollup_method: sum
                sign: raw
                format: currency
                fs_group: Revenue
        """)
        with pytest.raises(CatalogueError, match="Duplicate metric key"):
            load_catalogue(str(tmp_path))


# ---------------------------------------------------------------------------
# 8. Circular formula detection
# ---------------------------------------------------------------------------

class TestCircularFormula:
    def test_circular_metric_formulas(self, tmp_path):
        write_yml(tmp_path, "circular.yml", """
            domain: test
            source_view: semantic.v_test
            metrics:
              - key: base_m
                label: Base
                source_column: amount
                aggregation: sum
                rollup_method: sum
                sign: raw
                format: number
                fs_group: Test
              - key: a
                label: A
                formula: "b + base_m"
                format: number
                fs_group: Test
              - key: b
                label: B
                formula: "a + 1"
                format: number
                fs_group: Test
        """)
        with pytest.raises(CatalogueError, match="Circular dependency"):
            load_catalogue(str(tmp_path))


# ---------------------------------------------------------------------------
# 9. Missing formula reference
# ---------------------------------------------------------------------------

class TestMissingFormulaReference:
    def test_unknown_ref_raises(self, tmp_path):
        write_yml(tmp_path, "bad_ref.yml", """
            domain: test
            source_view: semantic.v_test
            metrics:
              - key: revenue
                label: Revenue
                source_column: amount
                aggregation: sum
                rollup_method: sum
                sign: abs
                format: currency
                fs_group: Revenue
              - key: bad_derived
                label: Bad
                formula: "revenue + nonexistent_metric"
                format: currency
                fs_group: Test
        """)
        with pytest.raises(CatalogueError, match="unknown metric key"):
            load_catalogue(str(tmp_path))


# ---------------------------------------------------------------------------
# 10. Invalid rollup_method
# ---------------------------------------------------------------------------

class TestInvalidRollupMethod:
    def test_invalid_rollup_raises(self, tmp_path):
        write_yml(tmp_path, "bad_rollup.yml", """
            domain: test
            source_view: semantic.v_test
            metrics:
              - key: revenue
                label: Revenue
                source_column: amount
                aggregation: sum
                rollup_method: invalid
                sign: abs
                format: currency
                fs_group: Revenue
        """)
        with pytest.raises(CatalogueError, match="rollup_method"):
            load_catalogue(str(tmp_path))


# ---------------------------------------------------------------------------
# 11. Statement with both lines and concat
# ---------------------------------------------------------------------------

class TestStatementBothLinesAndConcat:
    def test_both_raises(self, tmp_path):
        write_yml(tmp_path, "metrics.yml", """
            domain: test
            source_view: semantic.v_test
            metrics:
              - key: revenue
                label: Revenue
                source_column: amount
                aggregation: sum
                rollup_method: sum
                sign: abs
                format: currency
                fs_group: Revenue
        """)
        write_yml(tmp_path, "stmts.yml", """
            statements:
              bad_stmt:
                label: Bad Statement
                lines:
                  - revenue
                concat:
                  - bad_stmt
        """)
        with pytest.raises(CatalogueError, match="both 'lines' and 'concat'"):
            load_catalogue(str(tmp_path))


# ---------------------------------------------------------------------------
# 12. Circular statement concat
# ---------------------------------------------------------------------------

class TestCircularStatementConcat:
    def test_circular_concat_raises(self, tmp_path):
        write_yml(tmp_path, "metrics.yml", """
            domain: test
            source_view: semantic.v_test
            metrics:
              - key: revenue
                label: Revenue
                source_column: amount
                aggregation: sum
                rollup_method: sum
                sign: abs
                format: currency
                fs_group: Revenue
        """)
        write_yml(tmp_path, "stmts.yml", """
            statements:
              stmt_a:
                label: Statement A
                concat:
                  - stmt_b
              stmt_b:
                label: Statement B
                concat:
                  - stmt_a
        """)
        with pytest.raises(CatalogueError, match="Circular dependency"):
            load_catalogue(str(tmp_path))


# ---------------------------------------------------------------------------
# 15. Master dimension loading from real catalogue
# ---------------------------------------------------------------------------

class TestDimensionLoading:
    def test_cost_centre_dimension_loaded(self):
        cat = load_catalogue(str(CATALOGUE_DIR))
        assert "cost_centre" in cat.dimensions

    def test_cost_centre_is_leaf(self):
        cat = load_catalogue(str(CATALOGUE_DIR))
        dim = cat.dimensions["cost_centre"]
        assert isinstance(dim, Dimension)
        assert dim.label == "Cost Centre"
        assert dim.is_leaf
        assert not dim.is_derived
        assert not dim.is_ragged
        assert dim.source_table == "semantic.dim_cost_centre"
        assert dim.key_column == "cost_centre"

    def test_cost_centre_attributes(self):
        cat = load_catalogue(str(CATALOGUE_DIR))
        dim = cat.dimensions["cost_centre"]
        assert "name" in dim.attributes
        assert dim.attributes["name"].label == "Cost Centre Name"
        assert dim.display_attribute == "name"
        assert dim.source.attribute_mapping == {"name": "cost_centre_name"}

    def test_cost_centre_parents(self):
        cat = load_catalogue(str(CATALOGUE_DIR))
        dim = cat.dimensions["cost_centre"]
        assert "department" in dim.parents
        assert dim.parents["department"].source_column == "department"

    def test_department_is_derived(self):
        cat = load_catalogue(str(CATALOGUE_DIR))
        dim = cat.dimensions["department"]
        assert dim.is_derived
        assert not dim.is_leaf
        assert dim.derived_from.dimension == "cost_centre"
        assert dim.derived_from.source_column == "department"
        assert "division" in dim.parents

    def test_org_structure_is_ragged(self):
        cat = load_catalogue(str(CATALOGUE_DIR))
        dim = cat.dimensions["org_structure"]
        assert dim.is_ragged
        assert dim.label == "Organisational Structure"
        assert dim.root_label == "— All Cost Centres —"
        assert dim.leaf_dimension == "cost_centre"
        assert dim.ragged_source.type == "generated"
        assert len(dim.ragged_levels) == 3
        assert dim.ragged_levels[0].dimension == "division"
        assert dim.ragged_levels[0].display_prefix == "[D] "
        assert dim.ragged_levels[1].dimension == "department"
        assert dim.ragged_levels[1].display_prefix == "[BU] "
        assert dim.ragged_levels[2].dimension == "cost_centre"
        assert dim.ragged_levels[2].display_prefix == "[CC] "

    def test_transitive_closure_computed(self):
        cat = load_catalogue(str(CATALOGUE_DIR))
        dept = cat.dimensions["department"]
        assert "cost_centre" in dept._transitive
        tr = dept._transitive["cost_centre"]
        assert tr.leaf_dimension == "cost_centre"
        assert tr.source_table == "semantic.dim_cost_centre"
        assert tr.leaf_key_column == "cost_centre"
        assert tr.filter_column == "department"

    def test_division_transitive_closure(self):
        cat = load_catalogue(str(CATALOGUE_DIR))
        div = cat.dimensions["division"]
        assert "cost_centre" in div._transitive
        tr = div._transitive["cost_centre"]
        assert tr.filter_column == "division"

    def test_counterparty_cc_role_playing_leaf(self):
        # Role-playing dimension: a distinct leaf over its own view of the
        # cost-centre master, sharing the key column with the primary axis.
        cat = load_catalogue(str(CATALOGUE_DIR))
        dim = cat.dimensions["counterparty_cc"]
        assert dim.is_leaf
        assert dim.source_table == "semantic.dim_counterparty_cc"
        assert dim.key_column == cat.dimensions["cost_centre"].key_column == "cost_centre"
        assert "counterparty_department" in dim.parents

    def test_counterparty_hierarchy_transitive_closure(self):
        cat = load_catalogue(str(CATALOGUE_DIR))
        dept = cat.dimensions["counterparty_department"]
        assert dept.derived_from.dimension == "counterparty_cc"
        tr = dept._transitive["counterparty_cc"]
        assert tr.source_table == "semantic.dim_counterparty_cc"
        assert tr.filter_column == "department"
        div = cat.dimensions["counterparty_division"]
        assert div._transitive["counterparty_cc"].filter_column == "division"


# ---------------------------------------------------------------------------
# 16. Cube dimension mapping on domains
# ---------------------------------------------------------------------------

class TestCubeDimensionMapping:
    def test_pnl_domain_has_cube_dimensions(self):
        cat = load_catalogue(str(CATALOGUE_DIR))
        pnl = cat.domains["pnl"]
        assert len(pnl.dimensions) == 2

    def test_pnl_cube_dimension_fields(self):
        cat = load_catalogue(str(CATALOGUE_DIR))
        cd = cat.domains["pnl"].dimensions[0]
        assert isinstance(cd, CubeDimension)
        assert cd.key == "cost_centre"
        assert cd.label == "Cost Centre"
        assert cd.source == "cost_centre"

    def test_cube_dimension_references_master(self):
        cat = load_catalogue(str(CATALOGUE_DIR))
        cd = cat.domains["pnl"].dimensions[0]
        # ``key`` is the catalogue dimension name; ``source`` is the view column.
        assert cd.key in cat.dimensions

    def test_inline_federated_cube_dimension_loads(self, tmp_path):
        write_yml(tmp_path, "domain.yml", """
            domain: fed
            source_view: finance.gl_transactions_detail
            backend: customer_pg
            backend_kind: ibis
            versioned: false
            dimensions:
              - key: supplier_id
                label: Supplier
                source_inline: true
                filterable: false
            metrics:
              - key: amount
                label: Amount
                source_column: amount
                aggregation: sum
                rollup_method: sum
                sign: raw
                format: currency
                fs_group: GL
        """)

        cat = load_catalogue(str(tmp_path))

        cd = cat.domains["fed"].dimensions[0]
        assert cd.key == "supplier_id"
        # Inline source defaults to the key (column == axis name) when omitted.
        assert cd.source == "supplier_id"
        assert cd.source_inline is True
        assert cd.filterable is False

    def test_inline_cube_dimension_requires_ibis_domain(self, tmp_path):
        write_yml(tmp_path, "domain.yml", """
            domain: test
            source_view: semantic.v_test
            dimensions:
              - key: supplier_id
                label: Supplier
                source_inline: true
                filterable: false
            metrics:
              - key: amount
                label: Amount
                source_column: amount
                aggregation: sum
                rollup_method: sum
                sign: raw
                format: currency
                fs_group: GL
        """)

        with pytest.raises(CatalogueError, match="source_inline.*not an Ibis"):
            load_catalogue(str(tmp_path))

    def test_inline_cube_dimension_must_not_be_filterable(self, tmp_path):
        write_yml(tmp_path, "domain.yml", """
            domain: fed
            source_view: finance.gl_transactions_detail
            backend: customer_pg
            backend_kind: ibis
            versioned: false
            dimensions:
              - key: supplier_id
                label: Supplier
                source_inline: true
            metrics:
              - key: amount
                label: Amount
                source_column: amount
                aggregation: sum
                rollup_method: sum
                sign: raw
                format: currency
                fs_group: GL
        """)

        with pytest.raises(CatalogueError, match="filterable: false"):
            load_catalogue(str(tmp_path))

    def test_native_cube_dimension_requires_source(self, tmp_path):
        write_yml(tmp_path, "domain.yml", """
            domain: test
            source_view: semantic.v_test
            dimensions:
              - key: supplier_id
                label: Supplier
            metrics:
              - key: amount
                label: Amount
                source_column: amount
                aggregation: sum
                rollup_method: sum
                sign: raw
                format: currency
                fs_group: GL
        """)

        with pytest.raises(CatalogueError, match="must define source"):
            load_catalogue(str(tmp_path))


# ---------------------------------------------------------------------------
# 17. Dimension validation errors
# ---------------------------------------------------------------------------

class TestDimensionValidation:
    def test_no_type_raises(self, tmp_path):
        """Dimension with no source, derived_from, or ragged raises."""
        write_yml(tmp_path, "dimensions.yml", """
            dimensions:
              bad_dim:
                label: Bad
        """)
        with pytest.raises(CatalogueError, match="must have one of"):
            load_catalogue(str(tmp_path))

    def test_leaf_missing_table_raises(self, tmp_path):
        write_yml(tmp_path, "dimensions.yml", """
            dimensions:
              bad_dim:
                label: Bad
                source:
                  table: ""
                  key_column: id
        """)
        with pytest.raises(CatalogueError, match="source missing table"):
            load_catalogue(str(tmp_path))

    def test_leaf_missing_key_column_raises(self, tmp_path):
        write_yml(tmp_path, "dimensions.yml", """
            dimensions:
              bad_dim:
                label: Bad
                source:
                  table: some_table
                  key_column: ""
        """)
        with pytest.raises(CatalogueError, match="source missing key_column"):
            load_catalogue(str(tmp_path))

    def test_derived_from_unknown_dimension_raises(self, tmp_path):
        write_yml(tmp_path, "dimensions.yml", """
            dimensions:
              bad_dim:
                label: Bad
                derived_from:
                  dimension: nonexistent
                  source_column: col
        """)
        with pytest.raises(CatalogueError, match="derived_from references unknown"):
            load_catalogue(str(tmp_path))

    def test_derived_from_missing_source_column_raises(self, tmp_path):
        write_yml(tmp_path, "dimensions.yml", """
            dimensions:
              leaf:
                label: Leaf
                source:
                  table: some_table
                  key_column: id
              bad_dim:
                label: Bad
                derived_from:
                  dimension: leaf
                  source_column: ""
        """)
        with pytest.raises(CatalogueError, match="derived_from missing source_column"):
            load_catalogue(str(tmp_path))

    def test_parent_references_unknown_dimension_raises(self, tmp_path):
        write_yml(tmp_path, "dimensions.yml", """
            dimensions:
              leaf:
                label: Leaf
                source:
                  table: some_table
                  key_column: id
                parents:
                  nonexistent:
                    source_column: col
        """)
        with pytest.raises(CatalogueError, match="parent.*unknown dimension"):
            load_catalogue(str(tmp_path))

    def test_ragged_missing_leaf_dimension_raises(self, tmp_path):
        write_yml(tmp_path, "dimensions.yml", """
            dimensions:
              bad_rag:
                label: Bad Ragged
                ragged: true
                levels:
                  - dimension: something
                source:
                  type: generated
        """)
        with pytest.raises(CatalogueError, match="missing leaf_dimension"):
            load_catalogue(str(tmp_path))

    def test_ragged_last_level_mismatch_raises(self, tmp_path):
        write_yml(tmp_path, "dimensions.yml", """
            dimensions:
              leaf:
                label: Leaf
                source:
                  table: some_table
                  key_column: id
              other:
                label: Other
                derived_from:
                  dimension: leaf
                  source_column: other_col
              bad_rag:
                label: Bad Ragged
                ragged: true
                leaf_dimension: leaf
                levels:
                  - dimension: other
                source:
                  type: generated
        """)
        with pytest.raises(CatalogueError, match="last level.*must match leaf_dimension"):
            load_catalogue(str(tmp_path))

    def test_ragged_invalid_source_type_raises(self, tmp_path):
        write_yml(tmp_path, "dimensions.yml", """
            dimensions:
              leaf:
                label: Leaf
                source:
                  table: some_table
                  key_column: id
              bad_rag:
                label: Bad Ragged
                ragged: true
                leaf_dimension: leaf
                levels:
                  - dimension: leaf
                source:
                  type: invalid
        """)
        with pytest.raises(CatalogueError, match="(generated|provided)"):
            load_catalogue(str(tmp_path))

    def test_ragged_provided_missing_table_raises(self, tmp_path):
        write_yml(tmp_path, "dimensions.yml", """
            dimensions:
              leaf:
                label: Leaf
                source:
                  table: some_table
                  key_column: id
              bad_rag:
                label: Bad Ragged
                ragged: true
                leaf_dimension: leaf
                levels:
                  - dimension: leaf
                source:
                  type: provided
        """)
        with pytest.raises(CatalogueError, match="type='provided' but table is empty"):
            load_catalogue(str(tmp_path))

    def test_ragged_provided_with_table_passes(self, tmp_path):
        write_yml(tmp_path, "dimensions.yml", """
            dimensions:
              leaf:
                label: Leaf
                source:
                  table: some_table
                  key_column: id
              rag:
                label: Ragged
                ragged: true
                leaf_dimension: leaf
                root_label: "— All —"
                levels:
                  - dimension: leaf
                source:
                  type: provided
                  table: semantic.geo_rollup
        """)
        cat = load_catalogue(str(tmp_path))
        dim = cat.dimensions["rag"]
        assert dim.is_ragged
        assert dim.ragged_source.type == "provided"
        assert dim.ragged_source.table == "semantic.geo_rollup"

    def test_cube_dimension_unknown_key_raises(self, tmp_path):
        write_yml(tmp_path, "domain.yml", """
            domain: test
            source_view: semantic.v_test
            dimensions:
              - key: nonexistent_dimension
                label: Some
                source: some_col
            metrics:
              - key: revenue
                label: Revenue
                source_column: amount
                aggregation: sum
                rollup_method: sum
                sign: abs
                format: currency
                fs_group: Revenue
        """)
        with pytest.raises(CatalogueError, match="not a known catalogue dimension"):
            load_catalogue(str(tmp_path))

    def test_display_attribute_references_unknown_raises(self, tmp_path):
        write_yml(tmp_path, "dimensions.yml", """
            dimensions:
              bad_dim:
                label: Bad
                display_attribute: nonexistent
                source:
                  table: some_table
                  key_column: id
        """)
        with pytest.raises(CatalogueError, match="display_attribute.*not a defined attribute"):
            load_catalogue(str(tmp_path))

    def test_sort_attribute_references_unknown_raises(self, tmp_path):
        write_yml(tmp_path, "dimensions.yml", """
            dimensions:
              bad_dim:
                label: Bad
                attributes:
                  name: { label: Name }
                sort_attribute: nonexistent
                source:
                  table: some_table
                  key_column: id
                  attribute_mapping:
                    name: name_col
        """)
        with pytest.raises(CatalogueError, match="sort_attribute.*not a defined attribute"):
            load_catalogue(str(tmp_path))

    def test_display_attribute_valid_passes(self, tmp_path):
        write_yml(tmp_path, "dimensions.yml", """
            dimensions:
              good_dim:
                label: Good
                attributes:
                  name: { label: Name }
                  alias: { label: Alias }
                display_attribute: alias
                sort_attribute: name
                source:
                  table: some_table
                  key_column: id
                  attribute_mapping:
                    name: name_col
                    alias: alias_col
        """)
        cat = load_catalogue(str(tmp_path))
        dim = cat.dimensions["good_dim"]
        assert dim.display_attribute == "alias"
        assert dim.sort_attribute == "name"

    def test_duplicate_dimension_key_raises(self, tmp_path):
        write_yml(tmp_path, "dim1.yml", """
            dimensions:
              cost_centre:
                label: CC
                source:
                  table: some_table
                  key_column: id
        """)
        write_yml(tmp_path, "dim2.yml", """
            dimensions:
              cost_centre:
                label: CC Duplicate
                source:
                  table: other_table
                  key_column: other_id
        """)
        with pytest.raises(CatalogueError, match="Duplicate dimension key"):
            load_catalogue(str(tmp_path))

    def test_flat_leaf_dimension_passes(self, tmp_path):
        """A leaf dimension with no parents is flat — valid."""
        write_yml(tmp_path, "dimensions.yml", """
            dimensions:
              simple:
                label: Simple
                source:
                  table: some_table
                  key_column: id
        """)
        cat = load_catalogue(str(tmp_path))
        dim = cat.dimensions["simple"]
        assert dim.is_leaf
        assert not dim.is_hierarchical
        assert dim.parents == {}

    def test_cycle_in_parents_raises(self, tmp_path):
        write_yml(tmp_path, "dimensions.yml", """
            dimensions:
              a:
                label: A
                source:
                  table: t_a
                  key_column: id
                parents:
                  b:
                    source_column: b_col
              b:
                label: B
                derived_from:
                  dimension: a
                  source_column: b_col
                parents:
                  a:
                    source_column: a_col
        """)
        with pytest.raises(CatalogueError, match="Circular dependency"):
            load_catalogue(str(tmp_path))

    def test_attribute_mapping_unknown_attribute_raises(self, tmp_path):
        write_yml(tmp_path, "dimensions.yml", """
            dimensions:
              bad:
                label: Bad
                source:
                  table: some_table
                  key_column: id
                  attribute_mapping:
                    nonexistent: col
        """)
        with pytest.raises(CatalogueError, match="attribute_mapping key.*not found"):
            load_catalogue(str(tmp_path))


# ---------------------------------------------------------------------------
# 18. Multi-dimension cube (counterparty pattern)
# ---------------------------------------------------------------------------

class TestDimensionAttributes:
    def test_multiple_attributes_parsed(self, tmp_path):
        write_yml(tmp_path, "dimensions.yml", """
            dimensions:
              region:
                label: Region
                attributes:
                  name: { label: Region Name }
                  iso: { label: ISO Code }
                  alias: { label: Short Name }
                display_attribute: name
                source:
                  table: master_regions
                  key_column: region_code
                  attribute_mapping:
                    name: region_name
                    iso: region_iso_code
                    alias: region_short
        """)
        cat = load_catalogue(str(tmp_path))
        dim = cat.dimensions["region"]
        assert len(dim.attributes) == 3
        assert dim.attributes["name"].label == "Region Name"
        assert dim.attributes["iso"].label == "ISO Code"
        assert dim.source.attribute_mapping == {
            "name": "region_name",
            "iso": "region_iso_code",
            "alias": "region_short",
        }
        assert dim.display_attribute == "name"


# ---------------------------------------------------------------------------
# Plan Datasets — Loading
# ---------------------------------------------------------------------------

# The open-tier instance drops the plan-write config (plan_datasets.yml /
# planning_context.yml are dropped from the open instance):
# no write path ships in open, and plan reads go through live.fact_plan. These
# tests pin the Précis demo instance's plan-dataset contents, so they skip
# against the open export tree.
requires_plan_datasets = pytest.mark.skipif(
    not (CATALOGUE_DIR / "plan_datasets.yml").exists(),
    reason="instance carries no plan-write config (open-tier instance)",
)


@requires_plan_datasets
class TestPlanDatasetLoading:

    def test_gl_plan_loaded(self):
        cat = load_catalogue(str(CATALOGUE_DIR))
        assert "gl_plan" in cat.plan_datasets

    def test_gl_plan_fields(self):
        cat = load_catalogue(str(CATALOGUE_DIR))
        ds = cat.plan_datasets["gl_plan"]
        assert ds.key == "gl_plan"
        assert ds.label == "General Ledger Plan"
        assert ds.table == "planning.entries"
        assert ds.value_column == "delta_amount"
        assert ds.value_type == "Decimal(18,2)"

    def test_gl_plan_dimensions(self):
        cat = load_catalogue(str(CATALOGUE_DIR))
        ds = cat.plan_datasets["gl_plan"]
        assert len(ds.dimensions) == 2
        assert ds.dimensions[0].key == "account"
        assert ds.dimensions[0].source == "account"
        assert ds.dimensions[1].key == "cost_centre"
        assert ds.dimensions[1].source == "cost_centre"

    def test_plan_dataset_count(self):
        cat = load_catalogue(str(CATALOGUE_DIR))
        assert len(cat.plan_datasets) == 1


# ---------------------------------------------------------------------------
# Plan Datasets — Validation
# ---------------------------------------------------------------------------

class TestPlanDatasetValidation:

    def _base_yml(self, tmp_path):
        """Write minimal dimension files needed for validation."""
        write_yml(tmp_path, "dimensions.yml", """
            dimensions:
              cost_centre:
                label: Cost Centre
                source:
                  table: dim_cost_centre
                  key_column: cost_centre_id
                parents:
                  department:
                    source_column: department
              department:
                label: Department
                derived_from:
                  dimension: cost_centre
                  source_column: department
                parents:
                  division:
                    source_column: division
              division:
                label: Division
                derived_from:
                  dimension: cost_centre
                  source_column: division
        """)
    def test_valid_plan_dataset(self, tmp_path):
        self._base_yml(tmp_path)
        write_yml(tmp_path, "plan_datasets.yml", """
            plan_datasets:
              test_plan:
                label: Test Plan
                table: planning.test
                value_column: delta_amount
                value_type: "Decimal(18,2)"
                dimensions:
                  - key: cost_centre
                    source: cost_centre
        """)
        cat = load_catalogue(str(tmp_path))
        assert "test_plan" in cat.plan_datasets

    def test_missing_table_raises(self, tmp_path):
        self._base_yml(tmp_path)
        write_yml(tmp_path, "plan_datasets.yml", """
            plan_datasets:
              bad:
                label: Bad
                table: ""
                value_column: delta_amount
                dimensions:
                  - key: cc
                    source: cost_centre
        """)
        with pytest.raises(CatalogueError, match="missing table"):
            load_catalogue(str(tmp_path))

    def test_missing_value_column_raises(self, tmp_path):
        self._base_yml(tmp_path)
        write_yml(tmp_path, "plan_datasets.yml", """
            plan_datasets:
              bad:
                label: Bad
                table: planning.test
                value_column: ""
                dimensions:
                  - key: cc
                    source: cost_centre
        """)
        with pytest.raises(CatalogueError, match="missing value_column"):
            load_catalogue(str(tmp_path))

    def test_no_dimensions_raises(self, tmp_path):
        self._base_yml(tmp_path)
        write_yml(tmp_path, "plan_datasets.yml", """
            plan_datasets:
              bad:
                label: Bad
                table: planning.test
                value_column: delta_amount
                dimensions: []
        """)
        with pytest.raises(CatalogueError, match="at least one dimension"):
            load_catalogue(str(tmp_path))

    def test_duplicate_dimension_key_raises(self, tmp_path):
        self._base_yml(tmp_path)
        write_yml(tmp_path, "plan_datasets.yml", """
            plan_datasets:
              bad:
                label: Bad
                table: planning.test
                value_column: delta_amount
                dimensions:
                  - key: cc
                    source: cost_centre
                  - key: cc
                    source: cost_centre
        """)
        with pytest.raises(CatalogueError, match="duplicate dimension key"):
            load_catalogue(str(tmp_path))

    def test_unknown_source_dimension_raises(self, tmp_path):
        self._base_yml(tmp_path)
        write_yml(tmp_path, "plan_datasets.yml", """
            plan_datasets:
              bad:
                label: Bad
                table: planning.test
                value_column: delta_amount
                dimensions:
                  - key: region
                    source: nonexistent
        """)
        with pytest.raises(CatalogueError, match="unknown master dimension"):
            load_catalogue(str(tmp_path))

    def test_no_source_no_values_raises(self, tmp_path):
        self._base_yml(tmp_path)
        write_yml(tmp_path, "plan_datasets.yml", """
            plan_datasets:
              bad:
                label: Bad
                table: planning.test
                value_column: delta_amount
                dimensions:
                  - key: orphan
        """)
        with pytest.raises(CatalogueError, match="must have either"):
            load_catalogue(str(tmp_path))

    def test_both_source_and_values_raises(self, tmp_path):
        self._base_yml(tmp_path)
        write_yml(tmp_path, "plan_datasets.yml", """
            plan_datasets:
              bad:
                label: Bad
                table: planning.test
                value_column: delta_amount
                dimensions:
                  - key: cc
                    source: cost_centre
                    values: [a, b]
        """)
        with pytest.raises(CatalogueError, match="cannot have both"):
            load_catalogue(str(tmp_path))

    def test_inline_values_dimension(self, tmp_path):
        self._base_yml(tmp_path)
        write_yml(tmp_path, "plan_datasets.yml", """
            plan_datasets:
              test_plan:
                label: Test Plan
                table: planning.test
                value_column: delta_amount
                dimensions:
                  - key: rate_type
                    values: [tm, fixed_fee, retainer]
        """)
        cat = load_catalogue(str(tmp_path))
        dim = cat.plan_datasets["test_plan"].dimensions[0]
        assert dim.values == ["tm", "fixed_fee", "retainer"]
        assert dim.source == ""

    def test_level_override_valid(self, tmp_path):
        self._base_yml(tmp_path)
        write_yml(tmp_path, "plan_datasets.yml", """
            plan_datasets:
              test_plan:
                label: Test Plan
                table: planning.test
                value_column: delta_fte
                dimensions:
                  - key: department
                    source: cost_centre
                    level: department
        """)
        cat = load_catalogue(str(tmp_path))
        dim = cat.plan_datasets["test_plan"].dimensions[0]
        assert dim.level == "department"

    def test_level_override_invalid_raises(self, tmp_path):
        self._base_yml(tmp_path)
        write_yml(tmp_path, "plan_datasets.yml", """
            plan_datasets:
              bad:
                label: Bad
                table: planning.test
                value_column: delta_fte
                dimensions:
                  - key: region
                    source: cost_centre
                    level: nonexistent_level
        """)
        with pytest.raises(CatalogueError, match="level.*not found"):
            load_catalogue(str(tmp_path))

    def test_default_value_type(self, tmp_path):
        self._base_yml(tmp_path)
        write_yml(tmp_path, "plan_datasets.yml", """
            plan_datasets:
              test_plan:
                label: Test Plan
                table: planning.test
                value_column: delta_amount
                dimensions:
                  - key: cc
                    source: cost_centre
        """)
        cat = load_catalogue(str(tmp_path))
        assert cat.plan_datasets["test_plan"].value_type == "Decimal(18,2)"

    def test_duplicate_dataset_key_raises(self, tmp_path):
        self._base_yml(tmp_path)
        write_yml(tmp_path, "ds1.yml", """
            plan_datasets:
              same_key:
                label: First
                table: planning.a
                value_column: delta_amount
                dimensions:
                  - key: cc
                    source: cost_centre
        """)
        write_yml(tmp_path, "ds2.yml", """
            plan_datasets:
              same_key:
                label: Second
                table: planning.b
                value_column: delta_amount
                dimensions:
                  - key: cc
                    source: cost_centre
        """)
        with pytest.raises(CatalogueError, match="Duplicate plan dataset key"):
            load_catalogue(str(tmp_path))


# ---------------------------------------------------------------------------
# Variance effect + scenario display attributes
# ---------------------------------------------------------------------------

class TestVarianceEffect:
    def test_default_variance_effect(self):
        """variance_effect defaults to 'natural' when omitted."""
        cat = load_catalogue(str(CATALOGUE_DIR))
        assert cat.metrics["revenue"].variance_effect == "natural"

    def test_explicit_inverse(self):
        cat = load_catalogue(str(CATALOGUE_DIR))
        assert cat.metrics["direct_cost"].variance_effect == "inverse"

    def test_explicit_neutral(self):
        cat = load_catalogue(str(CATALOGUE_DIR))
        assert cat.metrics["billable_hours"].variance_effect == "neutral"

    def test_derived_metric_variance_effect(self):
        cat = load_catalogue(str(CATALOGUE_DIR))
        assert cat.metrics["avg_fte_total"].variance_effect == "neutral"
        assert cat.metrics["payroll_cost_per_fte"].variance_effect == "inverse"
        assert cat.metrics["gross_margin"].variance_effect == "natural"

    def test_invalid_variance_effect_raises(self, tmp_path):
        write_yml(tmp_path, "pnl.yml", """
            domain: pnl
            source_view: semantic.v_pnl
            metrics:
              - key: bad
                label: Bad
                source_column: amount
                aggregation: sum
                rollup_method: sum
                sign: raw
                format: currency
                fs_group: Test
                variance_effect: wrong
        """)
        with pytest.raises(CatalogueError, match="variance_effect"):
            load_catalogue(str(tmp_path))


# ---------------------------------------------------------------------------
# Semantic-reference normalization (live → semantic → catalogue)
# ---------------------------------------------------------------------------

class TestSemanticRefNormalization:
    """The catalogue reads only the semantic layer: object references resolve to
    `semantic.*`, a `live.`-qualified reference is rejected, and a federated
    (ibis) domain's source_view is left addressing its foreign backend."""

    _METRIC = """
              - key: amt
                label: Amount
                source_column: amount
                aggregation: sum
                rollup_method: sum
                sign: raw
                format: number
                fs_group: Test
    """

    def test_bare_dimension_source_resolves_to_semantic(self, tmp_path):
        write_yml(tmp_path, "dimensions.yml", """
            dimensions:
              cc:
                label: CC
                source:
                  table: dim_cost_centre
                  key_column: cost_centre
        """)
        cat = load_catalogue(str(tmp_path))
        assert cat.dimensions["cc"].source.table == "semantic.dim_cost_centre"

    def test_semantic_qualified_dimension_source_passes_through(self, tmp_path):
        write_yml(tmp_path, "dimensions.yml", """
            dimensions:
              cc:
                label: CC
                source:
                  table: semantic.dim_cc
                  key_column: cc
        """)
        cat = load_catalogue(str(tmp_path))
        assert cat.dimensions["cc"].source.table == "semantic.dim_cc"

    def test_live_qualified_dimension_source_rejected(self, tmp_path):
        write_yml(tmp_path, "dimensions.yml", """
            dimensions:
              cc:
                label: CC
                source:
                  table: live.dim_cc
                  key_column: cc
        """)
        with pytest.raises(CatalogueError, match="semantic"):
            load_catalogue(str(tmp_path))

    def test_bare_domain_source_view_resolves_to_semantic(self, tmp_path):
        write_yml(tmp_path, "gl.yml", "domain: gl\nsource_view: v_gl\nmetrics:" + self._METRIC)
        cat = load_catalogue(str(tmp_path))
        assert cat.domains["gl"].source_view == "semantic.v_gl"

    def test_live_qualified_domain_source_view_rejected(self, tmp_path):
        write_yml(tmp_path, "gl.yml", "domain: gl\nsource_view: live.fact_gl\nmetrics:" + self._METRIC)
        with pytest.raises(CatalogueError, match="semantic"):
            load_catalogue(str(tmp_path))

    def test_federated_source_view_left_foreign(self, tmp_path):
        write_yml(
            tmp_path, "wf.yml",
            "domain: wf\nsource_view: finance.worklog_metric_view\n"
            "backend_kind: ibis\nversioned: false\nmetrics:" + self._METRIC,
        )
        cat = load_catalogue(str(tmp_path))
        assert cat.domains["wf"].source_view == "finance.worklog_metric_view"

    def test_transitive_resolution_carries_semantic_source(self, tmp_path):
        write_yml(tmp_path, "dimensions.yml", """
            dimensions:
              cc:
                label: CC
                source:
                  table: dim_cost_centre
                  key_column: cost_centre
                parents:
                  dept:
                    source_column: department
              dept:
                label: Dept
                derived_from:
                  dimension: cc
                  source_column: department
        """)
        cat = load_catalogue(str(tmp_path))
        tr = cat.dimensions["dept"]._transitive["cc"]
        assert tr.source_table == "semantic.dim_cost_centre"
