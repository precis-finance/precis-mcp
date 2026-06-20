# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any, Literal, Optional, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr


class CatalogueError(Exception):
    """Raised when catalogue validation fails."""
    pass


# ---------------------------------------------------------------------------
# Enum literals — kept as module constants so error messages and JSON Schema
# stay aligned with the runtime Literal types.
# ---------------------------------------------------------------------------

Aggregation = Literal["sum", "count", "count_distinct", "avg", "min", "max"]
RollupMethod = Literal["sum", "avg", "closing"]
MetricFormat = Literal["currency", "percent", "number"]
MetricStyle = Literal["header", "default", "subtotal", "total", "ratio"]
VarianceEffect = Literal["natural", "inverse", "neutral"]
BackendKind = Literal["clickhouse", "ibis"]
Sign = Literal["raw", "abs", "negate"]
PredicateOp = Literal[
    "eq", "neq", "in", "not_in", "gt", "gte", "lt", "lte", "is_null", "is_not_null",
]
RaggedSourceType = Literal["generated", "provided"]

_PREDICATE_VALUE_OPS = {"eq", "neq", "gt", "gte", "lt", "lte"}
_PREDICATE_VALUES_OPS = {"in", "not_in"}
_PREDICATE_NULL_OPS = {"is_null", "is_not_null"}
_SAFE_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


# ---------------------------------------------------------------------------
# Models — Metrics
# ---------------------------------------------------------------------------

class MetricPredicate(BaseModel):
    """Portable row filter for a base metric. Covers simple predicates that
    compile to both ClickHouse SQL and Ibis.
    """
    model_config = ConfigDict(extra="forbid")

    column: str
    op: PredicateOp
    value: Any = None
    values: list[Any] = Field(default_factory=list)


class BaseMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    source_column: str
    aggregation: Aggregation
    rollup_method: RollupMethod
    sign: Sign
    format: MetricFormat
    fs_group: str
    domain: str = "pnl"
    style: MetricStyle = "default"
    indent: int = 0
    separator_above: bool = False
    hide_if_zero: bool = False
    scale_exempt: bool = False
    variance_effect: VarianceEffect = "natural"
    description: str = ""
    calculation_note: str = ""
    where: list[MetricPredicate] = Field(default_factory=list)


class DerivedMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    formula: str
    format: MetricFormat
    fs_group: str
    style: MetricStyle = "default"
    indent: int = 0
    separator_above: bool = False
    hide_if_zero: bool = False
    scale_exempt: bool = False
    variance_effect: VarianceEffect = "natural"
    description: str = ""
    calculation_note: str = ""


Metric = Union[BaseMetric, DerivedMetric]


# ---------------------------------------------------------------------------
# Models — Dimensions (EPM-aligned: first-class dimensions with bottom-up
# parent relationships and pre-computed transitive closure)
# ---------------------------------------------------------------------------

class DimensionAttribute(BaseModel):
    """An attribute on a dimension (e.g. name, code)."""
    model_config = ConfigDict(extra="forbid")

    label: str


class DimensionSource(BaseModel):
    """Source table for a leaf dimension — has its own master data."""
    model_config = ConfigDict(extra="forbid")

    table: str
    key_column: str
    attribute_mapping: dict[str, str] = Field(default_factory=dict)


class DerivedFrom(BaseModel):
    """Declares a derived dimension whose members come from a FK column
    on another dimension's source table."""
    model_config = ConfigDict(extra="forbid")

    dimension: str
    source_column: str


class ParentRelationship(BaseModel):
    """Single-hop parent link from child → parent dimension."""
    model_config = ConfigDict(extra="forbid")

    source_column: str


class RaggedLevel(BaseModel):
    """One level in a ragged hierarchy, ordered root → leaf."""
    model_config = ConfigDict(extra="forbid")

    dimension: str
    display_prefix: str = ""
    node_prefix: str = ""


class RaggedSource(BaseModel):
    """How a ragged hierarchy is materialised."""
    model_config = ConfigDict(extra="forbid")

    type: RaggedSourceType
    table: str = ""
    child_column: str = ""
    parent_column: str = ""


class TransitiveResolution(BaseModel):
    """Pre-computed info for resolving a filter on an ancestor dimension
    to leaf IDs of a descendant dimension.

    Example: division → cost_centre via the chain
        division → department (source_column='division' on dim_cost_centre)
        department → cost_centre (source_column='department' on dim_cost_centre)
    Flattened: SELECT cost_centre_id FROM gl.dim_cost_centre WHERE division = ?
    """
    model_config = ConfigDict(extra="forbid")

    leaf_dimension: str
    source_table: str
    leaf_key_column: str
    filter_column: str


class Dimension(BaseModel):
    """First-class dimension definition.

    Every dimension is one of three types:
      - **Leaf** (``source`` set): has its own master data table
      - **Derived** (``derived_from`` set): members come from a FK column
        on another dimension's source table
      - **Ragged** (``ragged=True``): a multi-level hierarchy that
        aggregates leaf members through prefixed node IDs

    Relationships are declared bottom-up via ``parents``.  The catalogue
    loader computes the transitive closure at load time so that any
    ancestor dimension can be used as a filter key.
    """
    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    attributes: dict[str, DimensionAttribute] = Field(default_factory=dict)
    display_attribute: str = ""
    sort_attribute: str = ""
    # --- Leaf dimensions ---
    source: Optional[DimensionSource] = None
    # --- Derived dimensions ---
    derived_from: Optional[DerivedFrom] = None
    # --- Parent relationships (leaf and derived only) ---
    parents: dict[str, ParentRelationship] = Field(default_factory=dict)
    # --- Ragged hierarchy dimensions ---
    ragged: bool = False
    root_label: str = ""
    leaf_dimension: str = ""
    ragged_levels: list[RaggedLevel] = Field(default_factory=list)
    ragged_source: Optional[RaggedSource] = None
    # --- Computed at load time — load-time cache, not config ---
    _transitive: dict[str, TransitiveResolution] = PrivateAttr(default_factory=dict)

    @property
    def is_leaf(self) -> bool:
        return self.source is not None

    @property
    def is_derived(self) -> bool:
        return self.derived_from is not None

    @property
    def is_ragged(self) -> bool:
        return self.ragged

    @property
    def source_table(self) -> str:
        if self.source:
            return self.source.table
        return ""

    @property
    def key_column(self) -> str:
        if self.source:
            return self.source.key_column
        return ""

    # Backwards compat — used by formatter.py
    @property
    def is_hierarchical(self) -> bool:
        return self.is_ragged or bool(self.parents)


class CubeDimension(BaseModel):
    """Binds a catalogue dimension to a column in a domain's source view.

    ``key`` is the catalogue dimension name the agent uses in both ``filters``
    and ``dimensions`` — for a native binding it must be a first-class master
    (or derived) dimension key. ``source`` is the physical column on the
    domain's source view the engine reads / groups by.

    Federated/Ibis domains can also declare source-only inline dimensions for
    reporting axes; those have no master data table and cannot be filtered in
    the phase-one implementation. For an inline dimension ``source`` is the
    view column (defaulting to ``key`` when omitted) and there is no master
    dimension to reference.
    """
    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    source: str = ""
    source_inline: bool = False
    filterable: bool = True


# ---------------------------------------------------------------------------
# Models — Statements / Catalogue
# ---------------------------------------------------------------------------

class Statement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    label: str
    description: str = ""
    lines: Optional[list[str]] = None    # metric keys + 'separator'
    concat: Optional[list[str]] = None   # statement names + 'separator'


class DomainCatalogue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: str
    source_view: str
    metrics: list[Metric]
    dimensions: list[CubeDimension] = Field(default_factory=list)
    versioned: bool = False                 # opt in with versioned: true for commit-aware plan domains (source view must carry commit_id)
    backend: str = "clickhouse_default"
    backend_kind: BackendKind = "clickhouse"
    inspect_enabled: bool = False
    inspect_columns: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Models — Plan Datasets (write-back)
# ---------------------------------------------------------------------------

class PlanDatasetDimension(BaseModel):
    """A dimension column in a plan dataset's write table.

    Either ``source`` (referencing a master dimension) or ``values`` (inline
    enum) must be set, but not both.  ``level`` overrides the default leaf
    binding when the plan grain is coarser than actuals (e.g. plan headcount
    at grade level, not individual employee).
    """
    model_config = ConfigDict(extra="forbid")

    key: str
    source: str = ""
    level: str = ""
    values: list[str] = Field(default_factory=list)


class PlanDataset(BaseModel):
    """A writable plan dataset — defines the grain and storage for plan entries.

    Framework columns (period, scenario, user_id, commit_id, inserted_at) are
    convention — shared by all plan tables and not declared here.  Only
    business dimensions and the value column vary per dataset.
    """
    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    table: str
    value_column: str
    value_type: str
    domain: str = ""
    dimensions: list[PlanDatasetDimension] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Catalogue — top-level container
# ---------------------------------------------------------------------------

class Catalogue(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    metrics: dict[str, Metric]           # key -> Metric
    scenarios: dict[str, Any]
    statements: dict[str, Statement]     # name -> Statement
    domains: dict[str, DomainCatalogue]  # domain name -> DomainCatalogue
    dimensions: dict[str, Dimension] = Field(default_factory=dict)
    plan_datasets: dict[str, PlanDataset] = Field(default_factory=dict)
    planning_context: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_names_from_formula(formula: str) -> set[str]:
    """Parse formula as a Python expression and return all Name nodes.

    Skips built-in names like 'abs' that are valid Python builtins.
    """
    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as exc:
        raise CatalogueError(f"Invalid formula syntax: {formula!r} — {exc}") from exc

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
    return names


_PYTHON_BUILTINS = {"abs", "round", "min", "max", "sum", "len", "int", "float", "bool"}


def _metric_refs(formula: str) -> set[str]:
    return _extract_names_from_formula(formula) - _PYTHON_BUILTINS


def _has_cycle(graph: dict[str, set[str]]) -> list[str] | None:
    """Kahn's algorithm — returns a cycle example if one exists, else None."""
    in_degree: dict[str, int] = {node: 0 for node in graph}
    for deps in graph.values():
        for dep in deps:
            if dep in in_degree:
                in_degree[dep] += 1

    queue = [n for n, d in in_degree.items() if d == 0]
    visited = 0
    while queue:
        node = queue.pop()
        visited += 1
        for dep in graph.get(node, set()):
            if dep not in in_degree:
                continue
            in_degree[dep] -= 1
            if in_degree[dep] == 0:
                queue.append(dep)

    cycle_nodes = [n for n, d in in_degree.items() if d > 0]
    return cycle_nodes if cycle_nodes else None


def _parse_metric_predicate(raw: Any) -> MetricPredicate:
    if not isinstance(raw, dict):
        raise CatalogueError(f"Metric predicate must be a mapping, got {type(raw).__name__}")
    column = raw.get("column", "")
    op = raw.get("op", "")
    values = raw.get("values", [])
    if values is None:
        values = []
    if not isinstance(values, list):
        raise CatalogueError(
            f"Metric predicate {column!r}/{op!r} field 'values' must be a list"
        )
    try:
        return MetricPredicate(
            column=column,
            op=op,
            value=raw.get("value"),
            values=list(values),
        )
    except Exception as exc:
        raise CatalogueError(
            f"Invalid metric predicate {column!r}/{op!r}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_metric(raw: dict, domain: str = "pnl") -> Metric:
    key = raw.get("key", "")

    try:
        if "formula" in raw:
            # Derived metric — must NOT have base-only fields
            return DerivedMetric(
                key=key,
                label=raw["label"],
                formula=raw["formula"],
                format=raw["format"],
                fs_group=raw["fs_group"],
                style=raw.get("style", "default"),
                indent=raw.get("indent", 0),
                separator_above=raw.get("separator_above", False),
                hide_if_zero=raw.get("hide_if_zero", False),
                scale_exempt=raw.get("scale_exempt", False),
                variance_effect=raw.get("variance_effect", "natural"),
                description=raw.get("description", ""),
                calculation_note=raw.get("calculation_note", ""),
            )

        if "source_filter" in raw:
            raise CatalogueError(
                f"BaseMetric {key!r}: 'source_filter' is no longer supported; "
                "use the structured 'where' predicate list instead"
            )
        raw_where = raw.get("where", [])
        if raw_where is None:
            raw_where = []
        if not isinstance(raw_where, list):
            raise CatalogueError(f"BaseMetric {key!r} field 'where' must be a list")
        where = [_parse_metric_predicate(p) for p in raw_where]

        return BaseMetric(
            key=key,
            label=raw["label"],
            source_column=raw["source_column"],
            aggregation=raw["aggregation"],
            rollup_method=raw["rollup_method"],
            sign=raw["sign"],
            format=raw["format"],
            fs_group=raw["fs_group"],
            domain=domain,
            style=raw.get("style", "default"),
            indent=raw.get("indent", 0),
            separator_above=raw.get("separator_above", False),
            hide_if_zero=raw.get("hide_if_zero", False),
            scale_exempt=raw.get("scale_exempt", False),
            variance_effect=raw.get("variance_effect", "natural"),
            description=raw.get("description", ""),
            calculation_note=raw.get("calculation_note", ""),
            where=where,
        )
    except CatalogueError:
        raise
    except KeyError as exc:
        raise CatalogueError(f"Metric {key!r} missing required field: {exc}") from exc
    except Exception as exc:
        raise CatalogueError(f"Invalid metric {key!r}: {exc}") from exc


def _parse_statement(name: str, raw: dict) -> Statement:
    try:
        return Statement(
            name=name,
            label=raw["label"],
            description=raw.get("description", ""),
            lines=raw.get("lines"),
            concat=raw.get("concat"),
        )
    except KeyError as exc:
        raise CatalogueError(f"Statement {name!r} missing required field: {exc}") from exc
    except Exception as exc:
        raise CatalogueError(f"Invalid statement {name!r}: {exc}") from exc


def _parse_dimension(key: str, raw: dict) -> Dimension:
    try:
        # --- Attributes ---
        attributes: dict[str, DimensionAttribute] = {}
        for attr_key, attr_raw in raw.get("attributes", {}).items():
            if isinstance(attr_raw, dict):
                attributes[attr_key] = DimensionAttribute(label=attr_raw.get("label", attr_key))
            else:
                attributes[attr_key] = DimensionAttribute(label=str(attr_raw))

        # --- Source (leaf dimensions) — only when it has 'table', not 'type' ---
        source: DimensionSource | None = None
        if "source" in raw and isinstance(raw["source"], dict) and "table" in raw["source"] and "type" not in raw["source"]:
            src_raw = raw["source"]
            source = DimensionSource(
                table=src_raw["table"],
                key_column=src_raw["key_column"],
                attribute_mapping=src_raw.get("attribute_mapping", {}),
            )

        # --- Derived from ---
        derived_from: DerivedFrom | None = None
        if "derived_from" in raw and isinstance(raw["derived_from"], dict):
            df_raw = raw["derived_from"]
            derived_from = DerivedFrom(
                dimension=df_raw["dimension"],
                source_column=df_raw["source_column"],
            )

        # --- Parents ---
        parents: dict[str, ParentRelationship] = {}
        for parent_key, parent_raw in raw.get("parents", {}).items():
            parents[parent_key] = ParentRelationship(
                source_column=parent_raw["source_column"],
            )

        # --- Ragged hierarchy ---
        is_ragged = raw.get("ragged", False)
        ragged_levels: list[RaggedLevel] = []
        ragged_source: RaggedSource | None = None
        root_label = ""
        leaf_dimension = ""

        if is_ragged:
            root_label = raw.get("root_label", "")
            leaf_dimension = raw.get("leaf_dimension", "")
            for rl_raw in raw.get("levels", []):
                ragged_levels.append(RaggedLevel(
                    dimension=rl_raw["dimension"],
                    display_prefix=rl_raw.get("display_prefix", ""),
                    node_prefix=rl_raw.get("node_prefix", ""),
                ))
            src_raw = raw.get("source", {})
            if isinstance(src_raw, dict):
                ragged_source = RaggedSource(
                    type=src_raw.get("type", "generated"),
                    table=src_raw.get("table", ""),
                    child_column=src_raw.get("child_column", ""),
                    parent_column=src_raw.get("parent_column", ""),
                )

        return Dimension(
            key=key,
            label=raw["label"],
            attributes=attributes,
            display_attribute=raw.get("display_attribute", ""),
            sort_attribute=raw.get("sort_attribute", ""),
            source=source,
            derived_from=derived_from,
            parents=parents,
            ragged=is_ragged,
            root_label=root_label,
            leaf_dimension=leaf_dimension,
            ragged_levels=ragged_levels,
            ragged_source=ragged_source,
        )
    except CatalogueError:
        raise
    except KeyError as exc:
        raise CatalogueError(f"Dimension {key!r} missing required field: {exc}") from exc
    except Exception as exc:
        raise CatalogueError(f"Invalid dimension {key!r}: {exc}") from exc


def _parse_cube_dimension(raw: dict) -> CubeDimension:
    try:
        source = raw.get("source", "")
        source_inline = raw.get("source_inline", False)
        # Inline axes name their own column via ``source``; default it to the
        # catalogue key when omitted so column == key for the simple case.
        if source_inline and not source:
            source = raw["key"]
        return CubeDimension(
            key=raw["key"],
            label=raw["label"],
            source=source,
            source_inline=source_inline,
            filterable=raw.get("filterable", True),
        )
    except KeyError as exc:
        raise CatalogueError(f"Cube dimension missing required field: {exc}") from exc
    except Exception as exc:
        raise CatalogueError(f"Invalid cube dimension: {exc}") from exc


def _parse_plan_dataset_dimension(raw: dict) -> PlanDatasetDimension:
    try:
        return PlanDatasetDimension(
            key=raw["key"],
            source=raw.get("source", ""),
            level=raw.get("level", ""),
            values=raw.get("values", []),
        )
    except KeyError as exc:
        raise CatalogueError(f"Plan dataset dimension missing required field: {exc}") from exc
    except Exception as exc:
        raise CatalogueError(f"Invalid plan dataset dimension: {exc}") from exc


def _parse_plan_dataset(key: str, raw: dict) -> PlanDataset:
    try:
        dims = [_parse_plan_dataset_dimension(d) for d in raw.get("dimensions", [])]
        return PlanDataset(
            key=key,
            label=raw["label"],
            table=raw["table"],
            value_column=raw["value_column"],
            value_type=raw.get("value_type", "Decimal(18,2)"),
            domain=raw.get("domain", ""),
            dimensions=dims,
        )
    except CatalogueError:
        raise
    except KeyError as exc:
        raise CatalogueError(f"Plan dataset {key!r} missing required field: {exc}") from exc
    except Exception as exc:
        raise CatalogueError(f"Invalid plan dataset {key!r}: {exc}") from exc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_catalogue(
    catalogue_dir: str,
    semantic_views_root: Optional[str] = None,
) -> Catalogue:
    """Load all .yml files from catalogue_dir, parse into Catalogue, validate.

    `semantic_views_root` is the directory holding `instance/semantic/views/*.sql`
    (per-deployment configurable). When provided, runs the source_view
    consistency check — every clickhouse-backed domain's `source_view` must
    reference a `.sql` file that exists on disk under that root. Skipped when
    None so callers that don't know the path (tests, some adhoc loads) still work.
    """
    cat_path = Path(catalogue_dir)
    if not cat_path.is_dir():
        raise CatalogueError(f"Catalogue directory not found: {catalogue_dir}")

    all_metrics: dict[str, Metric] = {}
    all_scenarios: dict[str, object] = {}
    all_statements: dict[str, Statement] = {}
    all_domains: dict[str, DomainCatalogue] = {}
    all_dimensions: dict[str, Dimension] = {}
    all_plan_datasets: dict[str, PlanDataset] = {}
    planning_context: dict | None = None

    # Two-pass: first pass collects master dimensions and domain cube-dimensions
    # (cube-dimensions reference master dimensions, validated in second pass)
    domain_raw_dims: dict[str, list[dict]] = {}

    for yml_file in sorted(cat_path.glob("*.yml")):
        with open(yml_file, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)

        if data is None:
            continue

        # --- Master dimensions (dimensions.yml or any file with top-level 'dimensions' dict) ---
        if "dimensions" in data and isinstance(data["dimensions"], dict):
            for key, raw_dim in data["dimensions"].items():
                if key in all_dimensions:
                    raise CatalogueError(
                        f"Duplicate dimension key {key!r} (found in {yml_file.name})"
                    )
                all_dimensions[key] = _parse_dimension(key, raw_dim)

        # --- Metrics (domain files) ---
        if "metrics" in data:
            domain_name = data.get("domain", yml_file.stem)
            source_view = data.get("source_view", "")
            domain_metrics: list[Metric] = []

            for raw_metric in data["metrics"]:
                metric = _parse_metric(raw_metric, domain=domain_name)
                if metric.key in all_metrics:
                    raise CatalogueError(
                        f"Duplicate metric key {metric.key!r} (found in {yml_file.name})"
                    )
                all_metrics[metric.key] = metric
                domain_metrics.append(metric)

            # Cube dimensions — list form in domain files
            cube_dims: list[CubeDimension] = []
            if "dimensions" in data and isinstance(data["dimensions"], list):
                for raw_cd in data["dimensions"]:
                    cube_dims.append(_parse_cube_dimension(raw_cd))
                domain_raw_dims[domain_name] = data["dimensions"]

            try:
                all_domains[domain_name] = DomainCatalogue(
                    domain=domain_name,
                    source_view=source_view,
                    metrics=domain_metrics,
                    dimensions=cube_dims,
                    versioned=data.get("versioned", False),
                    backend=data.get("backend", "clickhouse_default"),
                    backend_kind=data.get("backend_kind", "clickhouse"),
                    inspect_enabled=data.get("inspect_enabled", False),
                    inspect_columns=data.get("inspect_columns", []) or [],
                )
            except Exception as exc:
                raise CatalogueError(f"Invalid domain {domain_name!r}: {exc}") from exc

        # --- Statements ---
        if "statements" in data:
            for name, raw_stmt in data["statements"].items():
                all_statements[name] = _parse_statement(name, raw_stmt)

        # --- Plan datasets ---
        if "plan_datasets" in data:
            for key, raw_ds in data["plan_datasets"].items():
                if key in all_plan_datasets:
                    raise CatalogueError(
                        f"Duplicate plan dataset key {key!r} (found in {yml_file.name})"
                    )
                all_plan_datasets[key] = _parse_plan_dataset(key, raw_ds)

        # --- Planning context (at most one across all YAML files) ---
        if "planning_context" in data:
            planning_context = data["planning_context"]

    catalogue = Catalogue(
        metrics=all_metrics,
        scenarios=all_scenarios,
        statements=all_statements,
        domains=all_domains,
        dimensions=all_dimensions,
        plan_datasets=all_plan_datasets,
        planning_context=planning_context,
    )
    _normalize_semantic_refs(catalogue)
    _compute_transitive_closure(catalogue)
    validate_catalogue(catalogue)
    if semantic_views_root is not None:
        validate_source_views_against_semantic_root(catalogue, Path(semantic_views_root))
    return catalogue


def _resolve_semantic_ref(ref: str, context: str) -> str:
    """Resolve a catalogue object reference to a fully-qualified semantic object.

    The catalogue reads only the semantic layer (live → semantic → catalogue),
    so a bare name (``v_gl``) gains the ``semantic.`` schema and ``semantic.<x>``
    passes through. Any other schema — notably ``live.`` — is rejected: if the
    catalogue could point the engine at a raw table, the semantic indirection
    would be a fiction. Transform in ``instance/semantic/`` and reference that.
    """
    ref = ref.strip()
    if "." not in ref:
        return f"semantic.{ref}"
    schema = ref.split(".", 1)[0]
    if schema == "semantic":
        return ref
    raise CatalogueError(
        f"{context}: {ref!r} must reference a semantic object — a bare name "
        f"(resolved to 'semantic.<name>') or an explicit 'semantic.<name>'. "
        f"The catalogue reads only the semantic layer; land and transform in "
        f"instance/semantic/ and reference that, never schema {schema!r}."
    )


def _normalize_semantic_refs(catalogue: Catalogue) -> None:
    """Resolve every catalogue object reference to ``semantic.*`` in place.

    Covers clickhouse-backed domain ``source_view``, leaf-dimension
    ``source.table``, and provided ragged ``ragged_source.table``. Federated
    (``backend_kind != 'clickhouse'``) domain source views are left untouched —
    they address a foreign backend, not ``semantic.*``. Runs before transitive-
    closure computation so the resolved table flows into
    ``TransitiveResolution.source_table``.
    """
    for name, domain in catalogue.domains.items():
        if domain.backend_kind != "clickhouse":
            continue  # federated: source_view addresses the foreign backend
        if domain.source_view:
            domain.source_view = _resolve_semantic_ref(
                domain.source_view, f"domain {name!r} source_view"
            )
    for key, dim in catalogue.dimensions.items():
        if dim.source is not None and dim.source.table:
            dim.source.table = _resolve_semantic_ref(
                dim.source.table, f"dimension {key!r} source.table"
            )
        if dim.ragged_source is not None and dim.ragged_source.table:
            dim.ragged_source.table = _resolve_semantic_ref(
                dim.ragged_source.table, f"dimension {key!r} ragged_source.table"
            )


def _compute_transitive_closure(catalogue: Catalogue) -> None:
    """Walk parent chains and populate ``_transitive`` on each dimension.

    For every (ancestor_dim, leaf_dim) pair reachable through the parent chain,
    stores a ``TransitiveResolution`` that allows the filter resolver to go
    straight from ``ancestor_dim="value"`` → leaf IDs with a single SQL query.

    Also populates ``_transitive`` on ragged hierarchy dimensions so the filter
    resolver knows which leaf dimension they resolve to.
    """
    dims = catalogue.dimensions

    def _walk_parents(dim_key: str) -> list[tuple[str, str]]:
        """Return list of (ancestor_key, filter_column) for every ancestor
        reachable from ``dim_key`` via parent chains (BFS).

        All derived dimensions descending from the same leaf share the leaf's
        source table, so every ancestor's ``source_column`` is a column on
        that same table — making ``filter_column`` = ``source_column`` at
        every level.
        """
        ancestors: list[tuple[str, str]] = []
        visited: set[str] = {dim_key}
        queue: list[str] = [dim_key]
        while queue:
            current = dims[queue.pop(0)]
            for parent_key, parent_rel in current.parents.items():
                if parent_key in visited:
                    continue
                visited.add(parent_key)
                ancestors.append((parent_key, parent_rel.source_column))
                if parent_key in dims:
                    queue.append(parent_key)
        return ancestors

    # For each leaf dimension, compute transitive resolution for all ancestors
    for dim_key, dim in dims.items():
        if not dim.is_leaf or dim.source is None:
            continue
        ancestors = _walk_parents(dim_key)
        for ancestor_key, filter_column in ancestors:
            resolution = TransitiveResolution(
                leaf_dimension=dim_key,
                source_table=dim.source.table,
                leaf_key_column=dim.source.key_column,
                filter_column=filter_column,
            )
            # Store on the ancestor: "I can resolve to leaf_dim via this query"
            ancestor = dims.get(ancestor_key)
            if ancestor:
                ancestor._transitive[dim_key] = resolution

    # For ragged hierarchies, store a pointer to the leaf dimension
    for dim_key, dim in dims.items():
        if dim.is_ragged and dim.leaf_dimension:
            leaf_dim = dims.get(dim.leaf_dimension)
            if leaf_dim and leaf_dim.is_leaf and leaf_dim.source is not None:
                dim._transitive[dim.leaf_dimension] = TransitiveResolution(
                    leaf_dimension=dim.leaf_dimension,
                    source_table=leaf_dim.source.table,
                    leaf_key_column=leaf_dim.source.key_column,
                    filter_column="",  # ragged uses rollup view, not direct filter
                )


def validate_source_views_against_semantic_root(
    catalogue: Catalogue,
    semantic_views_root: Path,
) -> None:
    """Check every clickhouse-backed domain's `source_view` resolves to a `.sql` file.

    For every catalogue domain whose `backend_kind` is `clickhouse` (i.e.
    not federated), check that the `source_view` references a semantic view
    that exists on disk under `semantic_views_root`. The check walks the
    directory tree recursively and matches by filename stem
    (`<source_view_basename>.sql`).

    Ibis-backed federated domains are exempt because their source view
    lives on the customer-managed warehouse, not in the Précis-side
    `instance/semantic/views/` directory.
    """
    if not semantic_views_root.exists():
        # The semantic-views directory hasn't been initialised yet — silently skip;
        # startup will fail downstream when the engine tries to query the
        # missing view, with a clearer error.
        return

    view_files: set[str] = set()
    for sql_file in semantic_views_root.rglob("*.sql"):
        view_files.add(sql_file.stem)

    for domain_key, domain in catalogue.domains.items():
        if domain.backend_kind != "clickhouse":
            continue
        # `source_view` is typically `<schema>.<view>`; the on-disk file
        # stem is the bare view identifier.
        view_name = domain.source_view.rsplit(".", 1)[-1]
        if view_name not in view_files:
            raise CatalogueError(
                f"Domain {domain_key!r}: source_view {domain.source_view!r} "
                f"references semantic view {view_name!r} which has no "
                f"matching {view_name}.sql under {semantic_views_root}. "
                f"Every clickhouse-backed domain's source_view must map to "
                f"a semantic view file on disk."
            )


def validate_catalogue(catalogue: Catalogue) -> None:
    """Run cross-object validation checks. Per-field type/enum checks are
    enforced by Pydantic at construction time; this function covers rules
    that need the assembled catalogue (formula refs, statement composition,
    dimension cycles, plan dataset cross-refs).

    Raise CatalogueError on first failure.
    """
    metrics = catalogue.metrics
    statements = catalogue.statements

    # --- Metric-level cross-object checks ---
    for key, metric in metrics.items():
        if isinstance(metric, BaseMetric):
            for pred in metric.where:
                if not pred.column:
                    raise CatalogueError(f"BaseMetric {key!r} has predicate missing column")
                if not _SAFE_IDENTIFIER_RE.match(pred.column):
                    raise CatalogueError(
                        f"BaseMetric {key!r} has invalid predicate column {pred.column!r}"
                    )
                if pred.op in _PREDICATE_VALUE_OPS and pred.value is None:
                    raise CatalogueError(
                        f"BaseMetric {key!r} predicate {pred.column!r}/{pred.op!r} "
                        "must define 'value'"
                    )
                if pred.op in _PREDICATE_VALUES_OPS and not pred.values:
                    raise CatalogueError(
                        f"BaseMetric {key!r} predicate {pred.column!r}/{pred.op!r} "
                        "must define non-empty 'values'"
                    )
                if pred.op in _PREDICATE_NULL_OPS and (
                    pred.value is not None or pred.values
                ):
                    raise CatalogueError(
                        f"BaseMetric {key!r} predicate {pred.column!r}/{pred.op!r} "
                        "must not define 'value' or 'values'"
                    )

    # Rule: formula references must exist; no circular deps in metrics
    metric_dep_graph: dict[str, set[str]] = {}
    for key, metric in metrics.items():
        if isinstance(metric, DerivedMetric):
            refs = _metric_refs(metric.formula)
            unknown = refs - set(metrics.keys())
            if unknown:
                raise CatalogueError(
                    f"DerivedMetric {key!r} formula references unknown metric key(s): {sorted(unknown)}"
                )
            metric_dep_graph[key] = refs & set(metrics.keys())
        else:
            metric_dep_graph[key] = set()

    cycle = _has_cycle(metric_dep_graph)
    if cycle:
        raise CatalogueError(
            f"Circular dependency detected in metric formulas involving: {sorted(cycle)}"
        )

    # --- Statement-level checks ---
    for name, stmt in statements.items():
        # Either lines or concat, never both
        if stmt.lines is not None and stmt.concat is not None:
            raise CatalogueError(
                f"Statement {name!r} has both 'lines' and 'concat' — only one is allowed"
            )
        if stmt.lines is None and stmt.concat is None:
            raise CatalogueError(
                f"Statement {name!r} has neither 'lines' nor 'concat'"
            )

        # lines entries are metric keys or 'separator'
        if stmt.lines is not None:
            for entry in stmt.lines:
                if entry != "separator" and entry not in metrics:
                    raise CatalogueError(
                        f"Statement {name!r} lines entry {entry!r} is not a metric key or 'separator'"
                    )

        # concat entries are statement names or 'separator'
        if stmt.concat is not None:
            for entry in stmt.concat:
                if entry != "separator" and entry not in statements:
                    raise CatalogueError(
                        f"Statement {name!r} concat entry {entry!r} is not a statement name or 'separator'"
                    )

    # No circular references in statement concat
    stmt_dep_graph: dict[str, set[str]] = {}
    for name, stmt in statements.items():
        if stmt.concat is not None:
            stmt_dep_graph[name] = {e for e in stmt.concat if e != "separator"}
        else:
            stmt_dep_graph[name] = set()

    cycle = _has_cycle(stmt_dep_graph)
    if cycle:
        raise CatalogueError(
            f"Circular dependency detected in statement concat involving: {sorted(cycle)}"
        )

    # --- Dimension-level checks ---
    dimensions = catalogue.dimensions

    for key, dim in dimensions.items():
        # Every dimension must be exactly one type
        type_count = sum([dim.is_leaf, dim.is_derived, dim.is_ragged])
        if type_count == 0:
            raise CatalogueError(
                f"Dimension {key!r} must have one of: source, derived_from, or ragged=true"
            )
        if type_count > 1:
            raise CatalogueError(
                f"Dimension {key!r} has multiple types set (source/derived_from/ragged)"
            )

        # Leaf dimension checks
        if dim.is_leaf:
            assert dim.source is not None
            if not dim.source.table:
                raise CatalogueError(f"Dimension {key!r} source missing table")
            if not dim.source.key_column:
                raise CatalogueError(f"Dimension {key!r} source missing key_column")
            # attribute_mapping values must correspond to defined attributes
            for attr_name in dim.source.attribute_mapping:
                if attr_name not in dim.attributes:
                    raise CatalogueError(
                        f"Dimension {key!r}: attribute_mapping key {attr_name!r} "
                        f"not found in attributes"
                    )

        # display_attribute / sort_attribute must reference defined attributes
        if dim.display_attribute and dim.display_attribute not in dim.attributes:
            raise CatalogueError(
                f"Dimension {key!r}: display_attribute {dim.display_attribute!r} "
                f"is not a defined attribute"
            )
        if dim.sort_attribute and dim.sort_attribute not in dim.attributes:
            raise CatalogueError(
                f"Dimension {key!r}: sort_attribute {dim.sort_attribute!r} "
                f"is not a defined attribute"
            )

        # Derived dimension checks
        if dim.is_derived:
            assert dim.derived_from is not None
            ref = dim.derived_from.dimension
            if ref not in dimensions:
                raise CatalogueError(
                    f"Dimension {key!r}: derived_from references unknown "
                    f"dimension {ref!r}"
                )
            if not dim.derived_from.source_column:
                raise CatalogueError(
                    f"Dimension {key!r}: derived_from missing source_column"
                )

        # Parent relationship checks
        for parent_key in dim.parents:
            if parent_key not in dimensions:
                raise CatalogueError(
                    f"Dimension {key!r}: parent {parent_key!r} references "
                    f"unknown dimension"
                )

        # Ragged hierarchy checks
        if dim.is_ragged:
            if not dim.leaf_dimension:
                raise CatalogueError(
                    f"Ragged dimension {key!r} missing leaf_dimension"
                )
            if dim.leaf_dimension not in dimensions:
                raise CatalogueError(
                    f"Ragged dimension {key!r}: leaf_dimension {dim.leaf_dimension!r} "
                    f"not found"
                )
            leaf = dimensions[dim.leaf_dimension]
            if not leaf.is_leaf:
                raise CatalogueError(
                    f"Ragged dimension {key!r}: leaf_dimension {dim.leaf_dimension!r} "
                    f"is not a leaf dimension (must have source)"
                )
            if not dim.ragged_levels:
                raise CatalogueError(
                    f"Ragged dimension {key!r} must have at least one level"
                )
            # Last level must match leaf_dimension
            if dim.ragged_levels[-1].dimension != dim.leaf_dimension:
                raise CatalogueError(
                    f"Ragged dimension {key!r}: last level "
                    f"{dim.ragged_levels[-1].dimension!r} must match "
                    f"leaf_dimension {dim.leaf_dimension!r}"
                )
            # All levels must reference existing dimensions
            for rl in dim.ragged_levels:
                if rl.dimension not in dimensions:
                    raise CatalogueError(
                        f"Ragged dimension {key!r}: level references "
                        f"unknown dimension {rl.dimension!r}"
                    )
            # Source type check — ragged_source.type already constrained by
            # Literal at construction; here we just require table when 'provided'
            if dim.ragged_source and dim.ragged_source.type == "provided" and not dim.ragged_source.table:
                raise CatalogueError(
                    f"Ragged dimension {key!r}: source type='provided' "
                    f"but table is empty"
                )

    # Check for cycles in parent chains
    parent_graph: dict[str, set[str]] = {}
    for key, dim in dimensions.items():
        parent_graph[key] = set(dim.parents.keys())
    cycle = _has_cycle(parent_graph)
    if cycle:
        raise CatalogueError(
            f"Circular dependency detected in dimension parent chains "
            f"involving: {sorted(cycle)}"
        )

    # --- Domain / cube dimension checks ---
    for domain_name, domain in catalogue.domains.items():
        seen_inspect_columns: set[str] = set()
        for col in domain.inspect_columns:
            if not _SAFE_IDENTIFIER_RE.match(col):
                raise CatalogueError(
                    f"Domain {domain_name!r} has invalid inspect column {col!r}"
                )
            if col in seen_inspect_columns:
                raise CatalogueError(
                    f"Domain {domain_name!r} has duplicate inspect column {col!r}"
                )
            seen_inspect_columns.add(col)
        if domain.inspect_enabled and not domain.inspect_columns:
            raise CatalogueError(
                f"Domain {domain_name!r} has inspect_enabled=true but inspect_columns is empty"
            )
        if domain.backend_kind == "ibis":
            if domain.versioned:
                raise CatalogueError(
                    f"Domain {domain_name!r} uses backend_kind='ibis' but versioned=true. "
                    "Federated domains must be versioned=false"
                )
            for metric in domain.metrics:
                if isinstance(metric, BaseMetric):
                    if metric.aggregation != "sum":
                        raise CatalogueError(
                            f"BaseMetric {metric.key!r} in federated domain "
                            f"{domain_name!r} has unsupported aggregation "
                            f"{metric.aggregation!r}; phase one supports only 'sum'"
                        )
                    if metric.rollup_method != "sum":
                        raise CatalogueError(
                            f"BaseMetric {metric.key!r} in federated domain "
                            f"{domain_name!r} has unsupported rollup_method "
                            f"{metric.rollup_method!r}; phase one supports only 'sum'"
                        )

        for cd in domain.dimensions:
            if cd.source_inline:
                if domain.backend_kind != "ibis":
                    raise CatalogueError(
                        f"Domain {domain_name!r} cube dimension {cd.key!r} is "
                        "source_inline but the domain is not an Ibis federated domain"
                    )
                if cd.filterable:
                    raise CatalogueError(
                        f"Domain {domain_name!r} cube dimension {cd.key!r} is "
                        "source_inline and must set filterable: false"
                    )
                continue
            if not cd.source:
                raise CatalogueError(
                    f"Domain {domain_name!r} cube dimension {cd.key!r} must define "
                    "source (the view column) unless source_inline: true"
                )
            if cd.key not in dimensions:
                raise CatalogueError(
                    f"Domain {domain_name!r} cube dimension {cd.key!r} is not a "
                    f"known catalogue dimension"
                )

    # --- Plan dataset checks ---
    for ds_key, ds in catalogue.plan_datasets.items():
        if not ds.table:
            raise CatalogueError(f"Plan dataset {ds_key!r} missing table")
        if not ds.value_column:
            raise CatalogueError(f"Plan dataset {ds_key!r} missing value_column")
        if not ds.dimensions:
            raise CatalogueError(f"Plan dataset {ds_key!r} must have at least one dimension")
        if ds.domain and ds.domain not in catalogue.domains:
            raise CatalogueError(
                f"Plan dataset {ds_key!r} references unknown domain {ds.domain!r}. "
                f"Valid domains: {sorted(catalogue.domains.keys())}"
            )
        if ds.domain and catalogue.domains[ds.domain].backend_kind != "clickhouse":
            raise CatalogueError(
                f"Plan dataset {ds_key!r} references federated domain {ds.domain!r}; "
                "plan datasets must target ClickHouse domains"
            )

        seen_dim_keys: set[str] = set()
        for dim in ds.dimensions:
            if dim.key in seen_dim_keys:
                raise CatalogueError(
                    f"Plan dataset {ds_key!r} has duplicate dimension key {dim.key!r}"
                )
            seen_dim_keys.add(dim.key)

            has_source = bool(dim.source)
            has_values = bool(dim.values)
            if not has_source and not has_values:
                raise CatalogueError(
                    f"Plan dataset {ds_key!r}, dimension {dim.key!r}: "
                    f"must have either 'source' or 'values'"
                )
            if has_source and has_values:
                raise CatalogueError(
                    f"Plan dataset {ds_key!r}, dimension {dim.key!r}: "
                    f"cannot have both 'source' and 'values'"
                )
            if has_source and dim.source not in dimensions:
                raise CatalogueError(
                    f"Plan dataset {ds_key!r}, dimension {dim.key!r}: "
                    f"source {dim.source!r} references unknown master dimension"
                )
            if dim.level and has_source:
                # Validate level references an existing dimension (parent or self)
                if dim.level not in dimensions:
                    raise CatalogueError(
                        f"Plan dataset {ds_key!r}, dimension {dim.key!r}: "
                        f"level {dim.level!r} not found as a dimension"
                    )


def resolve_statement(catalogue: Catalogue, statement_name: str) -> list[str]:
    """Resolve a statement to a flat list of metric keys + 'separator' entries.

    Handles concat recursion. Raises CatalogueError on circular reference.
    """
    if statement_name not in catalogue.statements:
        raise CatalogueError(f"Unknown statement: {statement_name!r}")

    def _resolve(name: str, visiting: set[str]) -> list[str]:
        if name in visiting:
            raise CatalogueError(
                f"Circular reference detected resolving statement {name!r}"
            )
        stmt = catalogue.statements[name]
        if stmt.lines is not None:
            return list(stmt.lines)
        # concat — guaranteed non-None by validate_catalogue (every statement
        # has exactly one of lines/concat).
        assert stmt.concat is not None
        visiting = visiting | {name}
        result: list[str] = []
        for entry in stmt.concat:
            if entry == "separator":
                result.append("separator")
            else:
                result.extend(_resolve(entry, visiting))
        return result

    return _resolve(statement_name, set())
