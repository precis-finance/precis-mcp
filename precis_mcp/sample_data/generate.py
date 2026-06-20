# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
# pyright: reportArgumentType=false, reportCallIssue=false, reportOptionalSubscript=false
"""
Synthetic sample-data generator — a populated demo model for evaluation.
Seed: 42 (fully reproducible).

Generates 36 months of internally consistent FP&A data for a fictional IT
consultancy, lands it in the mock customer Postgres, drives every ingestion
binding end-to-end so `live.*` is populated through the same pipeline a real
deployment runs, and seeds the plan scenarios:

  1. Org hierarchy (cost centres)
  2. Chart of accounts
  3. Employee master data + cost history
  4. Client list
  5. Project master data + assignments
  6. Timesheets (36 months)
  7. Payroll (36 months)
  8. Revenue recognition (36 months, in-memory)
  9. Journal entries (36 months) + validation
 10. Budget scenario BUD-2026 + Forecast scenario FC-2026-Q1
 11. CRM accounts + opportunities (file-drop CSVs)
 12. Ingestion run (PG → ClickHouse `live.*`) + semantic views

Where the plan scenarios land is the seam to the Précis platform: `main()` takes a
`land_plan` callable, defaulting to `land_plan_fact_plan` (open tier —
static amounts in `live.fact_plan`).

Run: python -m precis_mcp.sample_data
"""

from __future__ import annotations

import os
import sys
import random
import math
import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import holidays as hol
from faker import Faker
from dotenv import load_dotenv
import psycopg
import clickhouse_connect

# ──────────────────────────────────────────────────────────────────────────────
# Seeding
# ──────────────────────────────────────────────────────────────────────────────
SEED = 42
random.seed(SEED)
np.random.seed(SEED)
fake = Faker("de_DE")
Faker.seed(SEED)

# ──────────────────────────────────────────────────────────────────────────────
# Environment / connections
# ──────────────────────────────────────────────────────────────────────────────
load_dotenv()

# Resolve *_FILE secrets (PGPASSWORD_FILE → PGPASSWORD, CHPASSWORD_FILE →
# CHPASSWORD) so this module works inside the api container where compose
# mounts file-secrets rather than setting plain env vars.
import precis_mcp.secrets  # noqa: E402,F401

PG_DSN = {
    "host": os.getenv("PGHOST", "localhost"),
    "port": int(os.getenv("PGPORT", 5432)),
    "user": os.getenv("PGUSER", "fpa_user"),
    "password": os.getenv("PGPASSWORD", ""),
    "dbname": os.getenv("PGDATABASE", "fpa_actuals"),
}

CH_HOST = os.getenv("CHHOST", "localhost")
CH_PORT = int(os.getenv("CHPORT", 8123))
CH_USER = os.getenv("CHUSER", "default")
CH_PASSWORD = os.getenv("CHPASSWORD", "")


# Repo root in both layouts (the Précis monorepo and the open mirror): this file lives at
# <root>/precis_mcp/sample_data/generate.py, so instance/ and scripts/ are
# siblings two levels up.
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def pg_conn():
    return psycopg.connect(**PG_DSN)


def ch_client():
    return clickhouse_connect.get_client(
        host=CH_HOST, port=CH_PORT, username=CH_USER, password=CH_PASSWORD
    )


def ensure_environment() -> None:
    """Bootstrap the two Postgres prerequisites so a fresh stack needs no
    manual steps before this module runs.

    1. The mock customer-source database (``PGDATABASE``, default
       ``fpa_actuals``) — created via the maintenance ``postgres`` database
       if absent.
    2. The platform schema (``load_history`` etc.) — every binding run the
       ingestion trigger drives writes load_history rows, so the open
       migrations must have been applied. Runs ``scripts/migrate.py --scope
       open`` (idempotent; creates the platform DB itself if missing).
    """
    try:
        pg_conn().close()
    except psycopg.OperationalError as exc:
        if PG_DSN["dbname"] not in str(exc) or "does not exist" not in str(exc):
            raise
        admin_dsn = dict(PG_DSN, dbname="postgres")
        with psycopg.connect(**admin_dsn, autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{PG_DSN["dbname"]}"')
        print(f"  Created mock-source database {PG_DSN['dbname']!r}")

    migrate = PROJECT_ROOT / "scripts" / "migrate.py"
    if migrate.is_file():
        import subprocess

        subprocess.run(
            [sys.executable, str(migrate), "--scope", "open"], check=True
        )
    else:
        print(
            "  WARNING: scripts/migrate.py not found — apply the platform "
            "migrations yourself before ingestion (load_history lives there)."
        )


# ──────────────────────────────────────────────────────────────────────────────
# Global constants
# ──────────────────────────────────────────────────────────────────────────────
ENTITY_ID = "ENT-001"
ACTUALS_START = datetime.date(2023, 1, 1)
ACTUALS_END = datetime.date(2026, 5, 31)
BUDGET_YEAR = 2026

DE_HOLIDAYS = hol.Germany(prov="NW")  # use NRW as representative state

SEASONAL_MULTIPLIER = {
    1: 1.02, 2: 1.00, 3: 1.00, 4: 1.00,
    5: 0.98, 6: 0.98, 7: 0.97, 8: 0.88,
    9: 1.00, 10: 0.96, 11: 0.93, 12: 0.75,
}

GRADE_UTIL = {
    "JR":    (0.75, 0.10),
    "MID":   (0.80, 0.08),
    "SR":    (0.75, 0.08),
    "MGR":   (0.55, 0.12),
    "DIR":   (0.30, 0.15),
    "ADMIN": (0.00, 0.00),
}

GRADE_COST_RANGE = {
    "JR":    (35_000, 45_000),
    "MID":   (50_000, 65_000),
    "SR":    (70_000, 90_000),
    "MGR":   (85_000, 105_000),
    "DIR":   (110_000, 140_000),
    "ADMIN": (40_000, 80_000),
}

GRADE_BILL_RANGE = {
    "JR":  (450, 600),
    "MID": (650, 850),
    "SR":  (900, 1_200),
    "MGR": (1_100, 1_400),
    "DIR": (1_500, 2_000),
}

CREATED_AT_DEFAULT = datetime.datetime(2022, 12, 1, 0, 0, 0)


# ──────────────────────────────────────────────────────────────────────────────
# Utility helpers
# ──────────────────────────────────────────────────────────────────────────────

def months_in_range(start: datetime.date, end: datetime.date):
    """Yield (year, month) tuples inclusive."""
    cur = datetime.date(start.year, start.month, 1)
    while cur <= end:
        yield cur.year, cur.month
        # advance one month
        if cur.month == 12:
            cur = datetime.date(cur.year + 1, 1, 1)
        else:
            cur = datetime.date(cur.year, cur.month + 1, 1)


def period_str(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def last_business_day(year: int, month: int) -> datetime.date:
    """Return last business day of month."""
    if month == 12:
        next_month = datetime.date(year + 1, 1, 1)
    else:
        next_month = datetime.date(year, month + 1, 1)
    d = next_month - datetime.timedelta(days=1)
    while d.weekday() >= 5:  # Sat=5, Sun=6
        d -= datetime.timedelta(days=1)
    return d


def business_days_in_month(year: int, month: int) -> int:
    """Count business days (Mon-Fri, excl. DE public holidays)."""
    if month == 12:
        next_m = datetime.date(year + 1, 1, 1)
    else:
        next_m = datetime.date(year, month + 1, 1)
    start = datetime.date(year, month, 1)
    count = 0
    d = start
    while d < next_m:
        if d.weekday() < 5 and d not in DE_HOLIDAYS:
            count += 1
        d += datetime.timedelta(days=1)
    return count


def holiday_factor(month: int) -> float:
    if month == 12:
        return 0.70
    if month == 8:
        return 0.85
    return 1.0


def rng_normal_clipped(mu: float, sigma: float, lo: float, hi: float) -> float:
    val = np.random.normal(mu, sigma)
    return float(np.clip(val, lo, hi))


def rng_uniform(lo: float, hi: float) -> float:
    return float(np.random.uniform(lo, hi))


def rng_lognormal(mu: float, sigma: float) -> float:
    return float(np.random.lognormal(mu, sigma))


# ──────────────────────────────────────────────────────────────────────────────
# STEP 1 — Organisational Hierarchy
# ──────────────────────────────────────────────────────────────────────────────

COST_CENTRES = [
    # Technology Services / Cloud & Infrastructure
    ("CC-CLOUD-01", "Cloud - AWS Team",                "Cloud & Infrastructure", "Technology Services", True),
    ("CC-CLOUD-02", "Cloud - Azure Team",              "Cloud & Infrastructure", "Technology Services", True),
    ("CC-CLOUD-03", "Cloud - DevOps Team",             "Cloud & Infrastructure", "Technology Services", True),
    ("CC-CLOUD-04", "Cloud - Platform Engineering",    "Cloud & Infrastructure", "Technology Services", True),
    # Technology Services / Software Engineering
    ("CC-SENG-01",  "Software - Frontend",             "Software Engineering",   "Technology Services", True),
    ("CC-SENG-02",  "Software - Backend",              "Software Engineering",   "Technology Services", True),
    ("CC-SENG-03",  "Software - Mobile",               "Software Engineering",   "Technology Services", True),
    ("CC-SENG-04",  "Software - QA",                   "Software Engineering",   "Technology Services", True),
    ("CC-SENG-05",  "Software - Data Engineering",     "Software Engineering",   "Technology Services", True),
    ("CC-SENG-06",  "Software - ML/AI",                "Software Engineering",   "Technology Services", True),
    # Technology Services / Cybersecurity
    ("CC-CSEC-01",  "Cybersecurity - Consulting",      "Cybersecurity",          "Technology Services", True),
    ("CC-CSEC-02",  "Cybersecurity - Managed Svcs",   "Cybersecurity",          "Technology Services", True),
    # Advisory & Consulting / Digital Transformation
    ("CC-DGTX-01",  "Digital - Strategy",              "Digital Transformation", "Advisory & Consulting", True),
    ("CC-DGTX-02",  "Digital - Change Mgmt",           "Digital Transformation", "Advisory & Consulting", True),
    ("CC-DGTX-03",  "Digital - Process",               "Digital Transformation", "Advisory & Consulting", True),
    # Advisory & Consulting / Data & Analytics
    ("CC-DANA-01",  "Data - BI",                       "Data & Analytics",       "Advisory & Consulting", True),
    ("CC-DANA-02",  "Data - Advanced Analytics",       "Data & Analytics",       "Advisory & Consulting", True),
    ("CC-DANA-03",  "Data - Data Governance",          "Data & Analytics",       "Advisory & Consulting", True),
    # Corporate / Finance & Controlling
    ("CC-FINC-01",  "Finance & Controlling",           "Finance & Controlling",  "Corporate", False),
    # Corporate / Human Resources
    ("CC-HRES-01",  "Human Resources",                 "Human Resources",        "Corporate", False),
    # Corporate / Marketing & Sales
    ("CC-MKTG-01",  "Marketing",                       "Marketing & Sales",      "Corporate", False),
    ("CC-MKTG-02",  "Sales",                           "Marketing & Sales",      "Corporate", False),
    # Corporate / G&A
    ("CC-GADM-01",  "General Administration",          "General & Administration","Corporate", False),
    ("CC-GADM-02",  "Office Management",               "General & Administration","Corporate", False),
]

CC_MAP = {cc[0]: cc for cc in COST_CENTRES}
BILLABLE_CCS  = [cc[0] for cc in COST_CENTRES if cc[4]]
CORPORATE_CCS = [cc[0] for cc in COST_CENTRES if not cc[4]]

# Intra-group recharge agreements: (provider_cc, [consumer_ccs], base_monthly_eur).
# Not posted to the GL or plan.
INTERCO_AGREEMENTS = [
    ("CC-CLOUD-04", ["CC-CLOUD-01", "CC-CLOUD-02", "CC-CLOUD-03",
                     "CC-SENG-01", "CC-SENG-02", "CC-SENG-03",
                     "CC-SENG-05", "CC-SENG-06"], 5500.0),   # internal platform engineering
    ("CC-DANA-03",  ["CC-DANA-01", "CC-DANA-02", "CC-SENG-05"], 3200.0),  # data governance
    ("CC-GADM-02",  BILLABLE_CCS, 2200.0),                   # facilities / office space
    ("CC-FINC-01",  ["CC-CLOUD-01", "CC-SENG-02", "CC-DGTX-01",
                     "CC-DANA-01", "CC-CSEC-01"], 2800.0),   # shared finance & controlling
    ("CC-HRES-01",  BILLABLE_CCS, 1500.0),                   # shared HR services
]


# ──────────────────────────────────────────────────────────────────────────────
# STEP 2 — Chart of Accounts
# ──────────────────────────────────────────────────────────────────────────────

ACCOUNTS = [
    # code, name, type, fs_line, normal_balance, parent_code
    ("4000", "Revenue",                              "HEADER",        "Revenue",    "CR", None),
    ("4100", "T&M Revenue",                          "REVENUE",       "Revenue",    "CR", "4000"),
    ("4200", "Fixed-Fee Revenue",                    "REVENUE",       "Revenue",    "CR", "4000"),
    ("4300", "Milestone Revenue",                    "REVENUE",       "Revenue",    "CR", "4000"),
    ("5000", "Direct Costs",                         "HEADER",        "DirectCost", "DR", None),
    ("5100", "Direct Labour - Salaries",             "DIRECT_COST",   "DirectCost", "DR", "5000"),
    ("5110", "Direct Labour - Employer Contributions","DIRECT_COST",  "DirectCost", "DR", "5000"),
    ("5200", "Subcontractor Costs",                  "DIRECT_COST",   "DirectCost", "DR", "5000"),
    ("5300", "Direct Project Expenses",              "DIRECT_COST",   "DirectCost", "DR", "5000"),
    ("6000", "Indirect Costs",                       "HEADER",        "Indirect",   "DR", None),
    ("6100", "Indirect Labour - Salaries",           "INDIRECT_COST", "Indirect",   "DR", "6000"),
    ("6110", "Indirect Labour - Employer Contributions","INDIRECT_COST","Indirect", "DR", "6000"),
    ("6200", "Training & Development",               "INDIRECT_COST", "Indirect",   "DR", "6000"),
    ("6300", "Recruitment Costs",                    "INDIRECT_COST", "Indirect",   "DR", "6000"),
    ("7000", "SG&A",                                 "HEADER",        "SGA",        "DR", None),
    ("7100", "Office Rent & Facilities",             "SGA",           "SGA",        "DR", "7000"),
    ("7200", "IT & Software Licences",               "SGA",           "SGA",        "DR", "7000"),
    ("7300", "Marketing & Events",                   "SGA",           "SGA",        "DR", "7000"),
    ("7400", "Professional Services (Legal, Audit)", "SGA",           "SGA",        "DR", "7000"),
    ("7500", "Travel & Entertainment",               "SGA",           "SGA",        "DR", "7000"),
    ("7600", "Depreciation & Amortisation",          "SGA",           "SGA",        "DR", "7000"),
    ("7700", "Insurance",                            "SGA",           "SGA",        "DR", "7000"),
    ("7800", "Admin Salaries",                       "SGA",           "SGA",        "DR", "7000"),
    ("7810", "Admin Employer Contributions",         "SGA",           "SGA",        "DR", "7000"),
    ("8000", "Other Income / Expense",               "HEADER",        "Other",      "DR", None),
    ("8100", "Interest Income",                      "OTHER_INCOME",  "Other",      "CR", "8000"),
    ("8200", "Bank Charges",                         "OTHER_EXPENSE", "Other",      "DR", "8000"),
    ("9000", "Statistical Accounts",                 "HEADER",        "Statistical","DR", None),
    ("9100", "Billable Hours",                        "STATISTICAL",   "Statistical","DR", "9000"),
    ("9110", "Total Hours",                           "STATISTICAL",   "Statistical","DR", "9000"),
    ("9200", "FTEs - Billable",                       "STATISTICAL",   "Statistical","DR", "9000"),
    ("9210", "FTEs - Overhead",                       "STATISTICAL",   "Statistical","DR", "9000"),
    ("1000", "Assets",                               "HEADER",        "BS",         "DR", None),
    ("1100", "Cash & Bank",                          "ASSET",         "BS",         "DR", "1000"),
    ("1200", "Accounts Receivable",                  "ASSET",         "BS",         "DR", "1000"),
    ("1300", "Prepaid Expenses",                     "ASSET",         "BS",         "DR", "1000"),
    ("1400", "Accrued Revenue",                      "ASSET",         "BS",         "DR", "1000"),
    ("2000", "Liabilities",                          "HEADER",        "BS",         "CR", None),
    ("2100", "Accounts Payable",                     "LIABILITY",     "BS",         "CR", "2000"),
    ("2200", "Accrued Expenses",                     "LIABILITY",     "BS",         "CR", "2000"),
    ("2300", "Payroll Liabilities",                  "LIABILITY",     "BS",         "CR", "2000"),
    ("2400", "Deferred Revenue",                     "LIABILITY",     "BS",         "CR", "2000"),
    ("2500", "VAT Payable",                          "LIABILITY",     "BS",         "CR", "2000"),
    ("3000", "Equity",                               "HEADER",        "BS",         "CR", None),
    ("3100", "Retained Earnings",                    "EQUITY",        "BS",         "CR", "3000"),
]

ACCT_MAP = {a[0]: a for a in ACCOUNTS}


# ──────────────────────────────────────────────────────────────────────────────
# STEP 3 — Employee Master Data
# ──────────────────────────────────────────────────────────────────────────────

ROLE_TITLES = {
    "JR":    ["Junior Developer", "Junior Analyst", "Junior Consultant",
               "Junior Cloud Engineer", "Junior Data Analyst"],
    "MID":   ["Developer", "Analyst", "Consultant", "Cloud Engineer",
               "Data Engineer", "QA Engineer"],
    "SR":    ["Senior Developer", "Senior Consultant", "Tech Lead",
               "Senior Cloud Architect", "Senior Data Engineer", "Senior Analyst"],
    "MGR":   ["Delivery Manager", "Engagement Manager", "Project Manager",
               "Program Manager"],
    "DIR":   ["Practice Director", "Head of Department", "Director of Engineering",
               "VP of Consulting"],
    "ADMIN": ["HR Manager", "Finance Controller", "Marketing Manager",
               "Office Manager", "Sales Manager", "Recruiter"],
}


def _annual_cost(grade: str) -> float:
    lo, hi = GRADE_COST_RANGE[grade]
    mid = (lo + hi) / 2
    base = rng_uniform(lo, hi)
    noise = np.random.normal(0, 0.03 * mid)
    return max(lo * 0.9, base + noise)


def _bill_rate(grade: str, annual_cost: float) -> Optional[float]:
    if grade == "ADMIN":
        return None
    # daily cost = annual / 220 working days
    daily_cost = annual_cost / 220
    markup = rng_uniform(2.5, 3.5)
    base = daily_cost * markup
    noise = np.random.normal(0, 0.05 * base)
    lo, hi = GRADE_BILL_RANGE[grade]
    return float(np.clip(base + noise, lo * 0.85, hi * 1.15))


def generate_employees():
    """
    Returns:
      employees: list of dicts matching hr.employees schema
      cost_history: list of (employee_id, effective_date, annual_cost_eur)
    """
    employees = []
    cost_history = []  # (employee_id, effective_date, annual_cost_eur)

    # Grade distribution across employees (~130 total)
    grade_plan = [
        ("JR",    25, BILLABLE_CCS),
        ("MID",   40, BILLABLE_CCS),
        ("SR",    30, BILLABLE_CCS),
        ("MGR",   12, BILLABLE_CCS),
        ("DIR",    8, BILLABLE_CCS),
        ("ADMIN", 15, CORPORATE_CCS),
    ]

    all_employees_raw = []
    for grade, count, cc_pool in grade_plan:
        for _ in range(count):
            all_employees_raw.append((grade, random.choice(cc_pool)))

    # Shuffle so IDs are not sequential by department
    random.shuffle(all_employees_raw)

    emp_id = 1
    for grade, cc_id in all_employees_raw:
        annual_cost = _annual_cost(grade)
        bill_rate = _bill_rate(grade, annual_cost)

        # Start date: 110 founding employees before 2023-01-01, rest spread 2023-2025
        if emp_id <= 110:
            # Founding cohort: random date 2020-01 to 2022-12
            start = fake.date_between(start_date=datetime.date(2020, 1, 1),
                                       end_date=datetime.date(2022, 12, 31))
        else:
            # Hiring ramp: prefer Q1 and Q3, fewer Q4
            year = random.choices([2023, 2024, 2025], weights=[15, 10, 8])[0]
            month = random.choices(
                range(1, 13),
                weights=[3, 2, 3, 2, 2, 2, 3, 2, 3, 2, 1, 1]
            )[0]
            max_day = 28 if month == 2 else (30 if month in [4, 6, 9, 11] else 31)
            start = datetime.date(year, month, random.randint(1, max_day))

        # Attrition: ~7-9% effective probability — keeps active headcount in 105-125 range
        attrition_rate = 0.15 if grade in ("JR", "MID") else 0.09
        has_left = random.random() < (attrition_rate * 0.6)

        end_date = None
        if has_left:
            latest_end = datetime.date(2025, 12, 31)
            # Founding cohort: only leave within the actuals period (not before 2023)
            if emp_id <= 110:
                earliest_end = datetime.date(2023, 1, 1)
            else:
                earliest_end = start + datetime.timedelta(days=180)
            if earliest_end < latest_end:
                # cluster end dates around month-ends
                end_approx = fake.date_between(start_date=earliest_end, end_date=latest_end)
                # snap to month-end
                if end_approx.month == 12:
                    end_date = datetime.date(end_approx.year, 12, 31)
                else:
                    end_date = datetime.date(end_approx.year, end_approx.month + 1, 1) - datetime.timedelta(days=1)

        first = fake.first_name()
        last = fake.last_name()
        # Ensure unique email with emp_id suffix
        email = f"{first.lower()}.{last.lower()}{emp_id}@novatech.eu"

        # SERVICES = delivery/billable staff (costs above gross margin: 5100/5110)
        # SUPPORT  = corporate/central staff (costs in SG&A: 7800/7810)
        employee_type = "SUPPORT" if grade == "ADMIN" else "SERVICES"

        emp = {
            "employee_id": emp_id,
            "employee_code": f"EMP-{emp_id:04d}",
            "first_name": first,
            "last_name": last,
            "email": email,
            "grade": grade,
            "employee_type": employee_type,
            "role_title": random.choice(ROLE_TITLES[grade]),
            "cost_centre_id": cc_id,
            "annual_cost_eur": round(annual_cost, 2),
            "daily_bill_rate_eur": round(bill_rate, 2) if bill_rate else None,
            "fte": 1.00,
            "start_date": start,
            "end_date": end_date,
            "is_active": end_date is None or end_date > datetime.date(2026, 3, 20),
            "currency": "EUR",
            "created_at": datetime.datetime(start.year, start.month, start.day, 9, 0, 0),
            "updated_at": datetime.datetime(start.year, start.month, start.day, 9, 0, 0),
        }
        employees.append(emp)

        # Cost history: starting cost at hire
        cost_history.append((emp_id, start, annual_cost))

        # Annual raises each January
        for raise_year in range(2024, 2027):
            # Only if employed > 12 months before raise_year-01-01
            raise_date = datetime.date(raise_year, 1, 1)
            if start < raise_date - datetime.timedelta(days=365):
                if end_date is None or end_date >= raise_date:
                    prev_cost = [c for c in cost_history if c[0] == emp_id][-1][2]
                    raise_pct = rng_uniform(0.02, 0.05)
                    new_cost = prev_cost * (1 + raise_pct)
                    cost_history.append((emp_id, raise_date, new_cost))

        # Update current annual_cost to latest
        latest = [c for c in cost_history if c[0] == emp_id][-1]
        employees[-1]["annual_cost_eur"] = round(latest[2], 2)

        emp_id += 1

    return employees, cost_history


def cost_at_month(cost_history_by_emp: dict, emp_id: int, year: int, month: int) -> float:
    """Return the annual cost applicable for a given employee in a given month."""
    target = datetime.date(year, month, 1)
    records = cost_history_by_emp.get(emp_id, [])
    # Last record with effective_date <= target
    applicable = [r for r in records if r[0] <= target]
    if not applicable:
        return 0.0
    return applicable[-1][1]


# ──────────────────────────────────────────────────────────────────────────────
# STEP 4 — Client List
# ──────────────────────────────────────────────────────────────────────────────

CLIENT_INDUSTRIES = [
    "Banking & Financial Services", "Insurance", "Manufacturing",
    "Retail & E-commerce", "Energy & Utilities", "Telecommunications",
    "Public Sector", "Healthcare & Pharma", "Logistics & Transport",
    "Media & Entertainment",
]


def generate_clients():
    """
    15 clients aligned with the project distribution weights used downstream.
    Tier 1 = top 3 (heaviest project allocation), Tier 2 = next 4,
    Tier 3 = remainder. Industry assigned at random.
    """
    clients = []
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    for i in range(1, 16):
        if i <= 3:
            tier = "TIER_1"
        elif i <= 7:
            tier = "TIER_2"
        else:
            tier = "TIER_3"
        clients.append({
            "client_id": f"CLI-{i:03d}",
            "client_name": fake.company(),
            "industry": random.choice(CLIENT_INDUSTRIES),
            "tier": tier,
            "country": "DE",
            "created_at": now,
        })
    return clients


# ──────────────────────────────────────────────────────────────────────────────
# STEP 5 — Project Master Data + Assignments
# ──────────────────────────────────────────────────────────────────────────────

PROJECT_TYPES = ["T&M"] * 66 + ["FIXED_FEE"] * 42 + ["MILESTONE"] * 12
PROJECT_NAMES = [
    "Cloud Migration Initiative", "ERP Modernisation", "Data Platform Build-Out",
    "Digital Transformation Programme", "Security Hardening Sprint",
    "DevOps Enablement", "ML Pipeline Development", "BI Dashboard Rollout",
    "Infrastructure Audit", "Agile Coaching Engagement",
    "API Gateway Implementation", "Microservices Refactor",
    "Data Governance Framework", "Network Security Assessment",
    "Change Management Programme", "Process Optimisation Study",
    "Mobile App Development", "Frontend Redesign", "QA Automation Framework",
    "Cloud Cost Optimisation", "AI Proof of Concept", "SAP S/4HANA Integration",
    "Azure Landing Zone", "AWS Well-Architected Review", "Penetration Testing",
    "GDPR Compliance Programme", "Incident Response Playbook",
    "Customer Data Platform", "Real-Time Analytics Engine",
    "Workforce Analytics Dashboard", "CRM Implementation", "Finance System Upgrade",
    "HR Self-Service Portal", "Chatbot Development", "IoT Data Integration",
    "Blockchain PoC", "Automated Reporting Suite", "Performance Engineering",
    "Enterprise Architecture Review", "Cloud-Native Transformation",
    "Data Quality Sprint", "Reporting Acceleration", "Identity & Access Review",
    "Kubernetes Migration", "Observability Stack Build", "ETL Modernisation",
    "Power BI Rollout", "Anomaly Detection PoC", "Cost Allocation Framework",
    "Security Operations Setup", "Supplier Onboarding Automation",
    "Procurement Analytics", "Talent Analytics Dashboard", "API Security Audit",
    "Cloud Governance Framework", "SaaS Integration Hub", "CI/CD Pipeline Build",
    "Data Lake Design", "Revenue Analytics Engine", "Capacity Planning Tool",
    "Workforce Planning Model", "Risk Register Automation",
]


def generate_projects(employees, clients):
    """Returns projects list and in-memory assignments."""
    projects = []
    assignments = []  # (employee_id, project_id, assign_start, assign_end, monthly_hours_target)

    managers = [e for e in employees if e["grade"] in ("MGR", "DIR")]
    billable_emps = [e for e in employees if e["grade"] != "ADMIN"]

    random.shuffle(PROJECT_TYPES)
    proj_names = PROJECT_NAMES.copy()
    random.shuffle(proj_names)

    # Assign clients: top 3 get ~40% of projects
    client_weights = [5, 4, 4, 3, 3, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2]  # 15 clients
    client_pool = random.choices(clients, weights=client_weights, k=len(PROJECT_TYPES))

    # Build a CC pool that guarantees at least 2 projects per billable CC before
    # randomly distributing the remainder — prevents any CC ending up with zero revenue.
    cc_pool_base = BILLABLE_CCS * 2  # 2 guaranteed per CC
    random.shuffle(cc_pool_base)
    cc_pool_extra = random.choices(BILLABLE_CCS, k=max(0, len(PROJECT_TYPES) - len(cc_pool_base)))
    cc_pool = cc_pool_base + cc_pool_extra
    random.shuffle(cc_pool)

    proj_id = 1
    for idx, ptype in enumerate(PROJECT_TYPES):
        # Duration
        if ptype == "T&M":
            duration_months = int(rng_normal_clipped(8, 3, 3, 18))
        elif ptype == "FIXED_FEE":
            duration_months = int(rng_normal_clipped(6, 2, 4, 12))
        else:  # MILESTONE
            duration_months = int(rng_normal_clipped(12, 4, 6, 24))

        # Start date: include 2022 so active projects exist from Jan 2023 onward
        year = random.choices([2022, 2023, 2024, 2025], weights=[8, 14, 10, 8])[0]
        month = random.choices(range(1, 13), weights=[3, 2, 3, 2, 2, 2, 3, 2, 3, 2, 1, 1])[0]
        start_date = datetime.date(year, month, 1)

        planned_end = datetime.date(
            start_date.year + (start_date.month + duration_months - 1) // 12,
            (start_date.month + duration_months - 1) % 12 + 1,
            28
        )

        # ~15% complete early
        if random.random() < 0.15:
            actual_end = planned_end - datetime.timedelta(days=random.randint(30, 60))
        else:
            actual_end = planned_end

        # Status based on end date
        today = datetime.date(2026, 3, 20)
        if actual_end < today:
            status = "COMPLETED"
        elif start_date > today:
            status = "PIPELINE"
        else:
            status = "ACTIVE"
        if random.random() < 0.05 and status == "ACTIVE":
            status = "ON_HOLD"

        # Revenue / budget figures — kept small so revenue is spread across many
        # CCs rather than concentrated in a few large projects.
        if ptype == "T&M":
            budget_hours = round(rng_uniform(50, 400), 1)
            avg_rate = rng_uniform(700, 1100)
            budget_rev = round(budget_hours / 8 * avg_rate, 2)
            contract_value = None
        elif ptype == "FIXED_FEE":
            budget_hours = None
            contract_value = round(rng_uniform(30_000, 300_000), 2)
            budget_rev = contract_value
        else:  # MILESTONE
            budget_hours = None
            contract_value = round(rng_uniform(80_000, 500_000), 2)
            budget_rev = contract_value

        pm = random.choice(managers)
        cc_id = cc_pool[idx]
        client = client_pool[idx]

        proj = {
            "project_id": proj_id,
            "project_code": f"PRJ-{year}-{proj_id:03d}",
            "project_name": proj_names[idx % len(proj_names)],
            "client_id": client["client_id"],
            "client_name": client["client_name"],
            "project_type": ptype,
            "status": status,
            "start_date": start_date,
            "end_date": actual_end if status == "COMPLETED" else None,
            "budget_hours": budget_hours,
            "budget_revenue_eur": budget_rev,
            "contract_value_eur": contract_value,
            "project_manager_id": pm["employee_id"],
            "cost_centre_id": cc_id,
            "currency": "EUR",
            "created_at": datetime.datetime(year, max(1, month - 1), 15),
            "updated_at": datetime.datetime(year, month, 1),
        }
        projects.append(proj)

        # Assign employees
        n_members = {"T&M": random.randint(3, 6), "FIXED_FEE": random.randint(2, 5), "MILESTONE": random.randint(4, 8)}[ptype]
        # Filter employees active during at least part of the project
        eligible = [
            e for e in billable_emps
            if e["start_date"] <= actual_end
            and (e["end_date"] is None or e["end_date"] >= start_date)
        ]
        if len(eligible) < n_members:
            eligible = billable_emps  # fallback

        team = random.sample(eligible, min(n_members, len(eligible)))
        for member in team:
            a_start = max(start_date, member["start_date"])
            a_end = actual_end
            if member["end_date"] and member["end_date"] < a_end:
                a_end = member["end_date"]
            if a_start >= a_end:
                continue
            monthly_hours = round(rng_uniform(40, 120))
            assignments.append((member["employee_id"], proj_id, a_start, a_end, monthly_hours))

        proj_id += 1

    return projects, assignments


def fill_assignment_gaps(employees, projects, assignments):
    """
    Post-processing pass after project-based assignment generation.

    The project team sizes (3-6 members per project) cover only ~25% of
    billable employee-months. This function fills the gap by assigning each
    unassigned active billable employee-month to a randomly active project
    with TARGET_COVERAGE probability, bringing company-wide utilisation to ~70%.
    """
    TARGET_COVERAGE = 0.97

    # Which (employee_id, year, month) tuples already have an assignment
    covered: set[tuple] = set()
    for emp_id, proj_id, a_start, a_end, mhours in assignments:
        for year, month in months_in_range(
            max(a_start, ACTUALS_START), min(a_end, ACTUALS_END)
        ):
            covered.add((emp_id, year, month))

    # Active project_ids per (year, month)
    proj_by_month: dict[tuple, list] = {}
    for proj in projects:
        p_end = min(proj["end_date"] or ACTUALS_END, ACTUALS_END)
        if proj["start_date"] > ACTUALS_END:
            continue
        for year, month in months_in_range(
            max(proj["start_date"], ACTUALS_START), p_end
        ):
            proj_by_month.setdefault((year, month), []).append(proj["project_id"])

    new_assignments = list(assignments)

    for emp in employees:
        if emp["grade"] == "ADMIN":
            continue
        emp_start = max(emp["start_date"], ACTUALS_START)
        emp_end = min(emp["end_date"] or ACTUALS_END, ACTUALS_END)
        if emp_start > emp_end:
            continue

        for year, month in months_in_range(emp_start, emp_end):
            if (emp["employee_id"], year, month) in covered:
                continue
            if random.random() > TARGET_COVERAGE:
                continue  # intentionally on bench this month
            active_projs = proj_by_month.get((year, month), [])
            if not active_projs:
                continue
            proj_id = random.choice(active_projs)
            mstart = datetime.date(year, month, 1)
            if month == 12:
                mend = datetime.date(year, 12, 31)
            else:
                mend = datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)
            mhours = round(rng_uniform(60, 130))
            new_assignments.append((emp["employee_id"], proj_id, mstart, mend, mhours))
            covered.add((emp["employee_id"], year, month))

    return new_assignments


# ──────────────────────────────────────────────────────────────────────────────
# STEP 6 — Timesheets
# ──────────────────────────────────────────────────────────────────────────────

def generate_timesheets(employees, assignments):
    """Returns list of timesheet row dicts."""
    timesheets = []
    ts_id = 1

    # Index: emp_id -> [(proj_id, a_start, a_end, monthly_hours)]
    emp_assignments: dict[int, list] = {}
    for emp_id, proj_id, a_start, a_end, mhours in assignments:
        emp_assignments.setdefault(emp_id, []).append((proj_id, a_start, a_end, mhours))

    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

    for year, month in months_in_range(ACTUALS_START, ACTUALS_END):
        period = period_str(year, month)
        bdays = business_days_in_month(year, month)
        hfactor = holiday_factor(month)
        seasonal = SEASONAL_MULTIPLIER[month]
        avail_hours = bdays * 8.0 * hfactor

        for emp in employees:
            # Active this month?
            month_start = datetime.date(year, month, 1)
            month_end = last_business_day(year, month)
            if emp["start_date"] > month_end:
                continue
            if emp["end_date"] and emp["end_date"] < month_start:
                continue

            grade = emp["grade"]
            util_target, util_std = GRADE_UTIL[grade]
            util_target_adj = util_target * seasonal

            # Actual utilisation
            actual_util = rng_normal_clipped(util_target_adj, util_std, 0.0, 1.0)
            billable_hours = actual_util * avail_hours

            # Get project assignments active this month
            active_assigns = [
                (pj, mh) for (pj, a_start, a_end, mh) in emp_assignments.get(emp["employee_id"], [])
                if a_start <= month_end and a_end >= month_start
            ]

            if not active_assigns or grade == "ADMIN":
                billable_hours = 0.0

            # Distribute billable hours across projects
            if active_assigns and billable_hours > 0:
                total_target = sum(mh for _, mh in active_assigns)
                for proj_id, mh in active_assigns:
                    share = mh / total_target if total_target > 0 else 1 / len(active_assigns)
                    proj_hours = round(billable_hours * share, 2)
                    if proj_hours <= 0:
                        continue
                    timesheets.append({
                        "timesheet_id": ts_id,
                        "employee_id": emp["employee_id"],
                        "project_id": proj_id,
                        "cost_centre_id": emp["cost_centre_id"],
                        "period": period,
                        "hours_worked": proj_hours,
                        "hours_billable": proj_hours,
                        "activity_type": "PROJECT",
                        "created_at": now,
                    })
                    ts_id += 1

            # Non-billable row
            non_bill = round(avail_hours - billable_hours, 2)
            if non_bill > 0:
                # Split into categories (we record one aggregated non-billable row)
                # Use a dominant category
                if billable_hours == 0 and grade != "ADMIN":
                    act = "BENCH"
                elif grade == "ADMIN":
                    act = "INTERNAL"
                else:
                    act = random.choices(
                        ["INTERNAL", "TRAINING", "ADMIN", "BENCH"],
                        weights=[20, 15, 12, 3]
                    )[0]
                timesheets.append({
                    "timesheet_id": ts_id,
                    "employee_id": emp["employee_id"],
                    "project_id": None,
                    "cost_centre_id": emp["cost_centre_id"],
                    "period": period,
                    "hours_worked": non_bill,
                    "hours_billable": 0.0,
                    "activity_type": act,
                    "created_at": now,
                })
                ts_id += 1

    return timesheets


# ──────────────────────────────────────────────────────────────────────────────
# STEP 7 — Payroll
# ──────────────────────────────────────────────────────────────────────────────

def generate_payroll(employees, cost_history):
    payroll = []
    pr_id = 1
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

    # Build cost history index: emp_id -> sorted list of (effective_date, annual_cost)
    ch_index: dict[int, list] = {}
    for emp_id, eff_date, annual_cost in cost_history:
        ch_index.setdefault(emp_id, []).append((eff_date, annual_cost))
    for emp_id in ch_index:
        ch_index[emp_id].sort(key=lambda x: x[0])

    # Determine bonus-eligible employees for each December (60% of SR+)
    bonus_eligible_by_year: dict[int, set] = {}
    for year in [2023, 2024, 2025]:
        eligible = [e["employee_id"] for e in employees if e["grade"] in ("SR", "MGR", "DIR")]
        random.shuffle(eligible)
        n_bonus = int(len(eligible) * 0.60)
        bonus_eligible_by_year[year] = set(eligible[:n_bonus])

    for year, month in months_in_range(ACTUALS_START, ACTUALS_END):
        period = period_str(year, month)
        month_start = datetime.date(year, month, 1)
        month_end = last_business_day(year, month)

        for emp in employees:
            if emp["start_date"] > month_end:
                continue
            if emp["end_date"] and emp["end_date"] < month_start:
                continue

            annual_cost = cost_at_month(ch_index, emp["employee_id"], year, month)
            gross = round(annual_cost / 12, 2)
            contributions = round(gross * 0.30, 2)
            bonus = 0.0

            if month == 12 and emp["grade"] in ("SR", "MGR", "DIR"):
                if emp["employee_id"] in bonus_eligible_by_year.get(year, set()):
                    bonus = round(gross * rng_uniform(0.05, 0.15), 2)

            total = round(gross + contributions + bonus, 2)

            payroll.append({
                "payroll_id": pr_id,
                "employee_id": emp["employee_id"],
                "cost_centre_id": emp["cost_centre_id"],
                "period": period,
                "gross_salary_eur": gross,
                "employer_contributions_eur": contributions,
                "bonus_eur": bonus,
                "total_cost_eur": total,
                "currency": "EUR",
                "created_at": now,
            })
            pr_id += 1

    return payroll


# ──────────────────────────────────────────────────────────────────────────────
# STEP 7b — Intercompany / intra-group recharges (standalone memo subledger)
# ──────────────────────────────────────────────────────────────────────────────

def generate_intercompany():
    """Intra-group recharge rows — one per (provider → consumer → month).

    Shared-service and corporate cost centres recharge consuming delivery teams
    monthly, with seasonal + lognormal variation. ``cost_centre_id`` is the
    charged (consumer) centre; ``counterparty_cc_id`` is the providing centre.
    Not posted to the GL or plan.
    """
    rows = []
    rid = 1
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    for year, month in months_in_range(ACTUALS_START, ACTUALS_END):
        period = period_str(year, month)
        seasonal = SEASONAL_MULTIPLIER[month]
        for provider, consumers, base in INTERCO_AGREEMENTS:
            for consumer in consumers:
                if consumer == provider:
                    continue
                amount = round(base * seasonal * rng_lognormal(0.0, 0.15), 2)
                rows.append({
                    "intercompany_id": rid,
                    "cost_centre_id": consumer,
                    "counterparty_cc_id": provider,
                    "period": period,
                    "amount": amount,
                    "currency": "EUR",
                    "created_at": now,
                })
                rid += 1
    return rows


# ──────────────────────────────────────────────────────────────────────────────
# STEP 8 — Revenue Subledger (project × period grain)
# ──────────────────────────────────────────────────────────────────────────────
#
# Memo subledger: per-project recognised revenue, recognised cost, billings,
# WIP/deferred balance and % complete. Reconciles to GL revenue at the
# project×period grain (revenue side) and to GL direct cost at the
# CC×period grain (cost side — subledger captures only billable project
# effort, never bench / indirect).
#
# Recognition rules:
#   T&M       — revenue = billable_hours × weighted bill rate
#               cost    = hours_worked   × employee daily cost / 8
#               billing — monthly, lagged by one period (net-30 cadence)
#   FIXED_FEE — revenue = contract × ΔPOC; POC = cum_cost / EAC (cost-to-cost)
#               cost    = same effort × cost rate
#               billing — 30% kickoff / 40% mid / 30% at completion
#   MILESTONE — revenue recognised on milestone delivery (random schedule
#               around planned dates); cost recognised as effort is incurred
#               billing — same period as milestone delivery


def _employee_daily_cost(emp, year, month, ch_index):
    """Return the effective daily cost rate for an employee in the given month."""
    annual = cost_at_month(ch_index, emp["employee_id"], year, month)
    if annual <= 0:
        annual = emp["annual_cost_eur"] or 0
    # 220 working days/year, fully-loaded with 30% employer contributions
    return (annual * 1.30) / 220.0


def compute_revenue_subledger(projects, timesheets, employees, cost_history):
    """
    Returns a list of subledger row dicts, one per (project_id, period).

    Each row carries flow metrics (revenue/cost/billings recognised this
    period) AND stock metrics (cum totals, WIP, % complete) at period close.
    """
    emp_map = {e["employee_id"]: e for e in employees}
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

    # Build cost history index for daily-cost lookups
    ch_index: dict[int, list] = {}
    for emp_id, eff_date, annual_cost in cost_history:
        ch_index.setdefault(emp_id, []).append((eff_date, annual_cost))
    for emp_id in ch_index:
        ch_index[emp_id].sort(key=lambda x: x[0])

    # Index timesheets by (project_id, period)
    ts_by_pp: dict[tuple, list] = {}
    for ts in timesheets:
        ts_by_pp.setdefault((ts["project_id"], ts["period"]), []).append(ts)

    # Pre-compute per-project effort + cost per period (used by all methods)
    def period_effort_and_cost(pid, year, month):
        period = period_str(year, month)
        rows = ts_by_pp.get((pid, period), [])
        hours_worked = 0.0
        hours_billable = 0.0
        cost = 0.0
        weighted_rate_num = 0.0
        weighted_rate_den = 0.0
        for ts in rows:
            emp = emp_map.get(ts["employee_id"])
            if not emp:
                continue
            h_w = float(ts["hours_worked"])
            h_b = float(ts["hours_billable"])
            hours_worked += h_w
            hours_billable += h_b
            daily_cost = _employee_daily_cost(emp, year, month, ch_index)
            cost += h_w * (daily_cost / 8.0)
            rate = float(emp["daily_bill_rate_eur"] or 0)
            weighted_rate_num += h_b * rate
            weighted_rate_den += h_b
        wavg_rate = (weighted_rate_num / weighted_rate_den) if weighted_rate_den > 0 else 0.0
        return period, hours_worked, hours_billable, cost, wavg_rate

    # ── Pre-compute milestone schedules for MILESTONE projects ──────────────
    # Each milestone: (period, value). Drawn once per project.
    milestone_schedule: dict[int, list] = {}
    for proj in projects:
        if proj["project_type"] != "MILESTONE":
            continue
        pid = proj["project_id"]
        start = proj["start_date"]
        end_date = proj["end_date"] or ACTUALS_END
        contract = float(proj["contract_value_eur"] or 0)

        n_milestones = random.randint(3, 5)
        values = []
        remaining = contract
        for _ in range(n_milestones - 1):
            v = round(remaining * rng_uniform(0.15, 0.35), 2)
            values.append(v)
            remaining -= v
        values.append(round(remaining, 2))

        dur_months = max(1, int((end_date - start).days / 30))
        step = max(1, dur_months // n_milestones)
        events: list[tuple] = []
        for i, val in enumerate(values):
            planned_offset = step * (i + 1)
            roll = random.random()
            if roll < 0.70:
                offset = planned_offset
            elif roll < 0.95:
                offset = planned_offset + random.randint(1, 2)
            else:
                continue  # cancelled
            ms_date = start + datetime.timedelta(days=offset * 30)
            if ms_date > end_date or ms_date > ACTUALS_END:
                continue
            events.append((period_str(ms_date.year, ms_date.month), val))
        milestone_schedule[pid] = events

    # ── EAC for fixed-fee projects (frozen at start; cost-to-cost POC) ──────
    fixed_fee_eac: dict[int, float] = {}
    for proj in projects:
        if proj["project_type"] != "FIXED_FEE":
            continue
        pid = proj["project_id"]
        team_emp_ids = list({ts["employee_id"] for ts in timesheets if ts["project_id"] == pid})
        if not team_emp_ids:
            continue
        daily_costs = []
        for eid in team_emp_ids:
            emp = emp_map.get(eid)
            if emp:
                daily_costs.append((emp["annual_cost_eur"] or 0) / 220 * 1.30)
        avg_daily = sum(daily_costs) / len(daily_costs) if daily_costs else 500
        avg_hourly = avg_daily / 8
        fixed_fee_eac[pid] = avg_hourly * (proj["budget_hours"] or 2000) * 1.30

    # ── Walk each project across its active months ─────────────────────────
    rows: list[dict] = []

    for proj in projects:
        pid = proj["project_id"]
        ptype = proj["project_type"]
        start = proj["start_date"]
        end_date = proj["end_date"] or ACTUALS_END
        contract = float(proj["contract_value_eur"] or 0)
        cc_id = proj["cost_centre_id"]
        client_id = proj["client_id"]

        if ptype == "T&M":
            recognition_method = "TM"
        elif ptype == "FIXED_FEE":
            recognition_method = "POC_COST"
        else:
            recognition_method = "MILESTONE"

        active_months = list(months_in_range(
            max(start, ACTUALS_START), min(end_date, ACTUALS_END)
        ))
        if not active_months:
            continue

        cum_revenue = 0.0
        cum_cost = 0.0
        cum_billed = 0.0
        prev_pct = 0.0
        # T&M billing = previous period's revenue (1-month lag)
        prev_tm_revenue = 0.0
        # Fixed-fee billing schedule: 30% at first month, 40% at midpoint, 30% at last month
        ff_total_months = len(active_months)
        ff_kickoff_idx = 0
        ff_mid_idx = ff_total_months // 2
        ff_close_idx = ff_total_months - 1

        for idx, (year, month) in enumerate(active_months):
            period, hours_worked, hours_billable, cost_recog, wavg_rate = (
                period_effort_and_cost(pid, year, month)
            )

            # ── Revenue recognition ────────────────────────────────────────
            if ptype == "T&M":
                revenue_recog = (hours_billable / 8.0) * wavg_rate if hours_billable > 0 else 0.0
                # Effective contract for T&M = cumulative billed-hour value (open ceiling)
                contract_effective = 0.0
            elif ptype == "FIXED_FEE":
                eac = fixed_fee_eac.get(pid, 0.0)
                if eac > 0:
                    pct = min((cum_cost + cost_recog) / eac, 1.0)
                else:
                    pct = 0.0
                revenue_recog = max(0.0, contract * (pct - prev_pct))
                prev_pct = pct
                contract_effective = contract
            else:  # MILESTONE
                events = milestone_schedule.get(pid, [])
                revenue_recog = sum(v for p, v in events if p == period)
                contract_effective = contract

            # ── Billing ────────────────────────────────────────────────────
            if ptype == "T&M":
                amount_billed = prev_tm_revenue
                prev_tm_revenue = revenue_recog
            elif ptype == "FIXED_FEE":
                amount_billed = 0.0
                if idx == ff_kickoff_idx:
                    amount_billed += round(contract * 0.30, 2)
                if idx == ff_mid_idx and ff_mid_idx != ff_kickoff_idx:
                    amount_billed += round(contract * 0.40, 2)
                if idx == ff_close_idx and ff_close_idx not in (ff_kickoff_idx, ff_mid_idx):
                    # final cleanup: contract minus already billed
                    amount_billed += round(contract - cum_billed - amount_billed, 2)
            else:  # MILESTONE — bill the milestone in the same period it's delivered
                amount_billed = revenue_recog

            # ── Update running totals ──────────────────────────────────────
            cum_revenue += revenue_recog
            cum_cost += cost_recog
            cum_billed += amount_billed

            wip_balance = cum_revenue - cum_billed

            # % complete display: T&M doesn't have a meaningful % complete;
            # POC method already tracks it; milestone uses cum_billed/contract.
            if ptype == "T&M":
                pct_complete = 0.0
            elif ptype == "FIXED_FEE":
                pct_complete = prev_pct
            else:
                pct_complete = (cum_revenue / contract) if contract > 0 else 0.0
            pct_complete = max(0.0, min(1.0, pct_complete))

            # Skip months with zero everything (project not actually started yet)
            if (hours_worked == 0 and hours_billable == 0
                    and revenue_recog == 0 and amount_billed == 0):
                continue

            etc_cost = max(0.0, fixed_fee_eac.get(pid, 0.0) - cum_cost) if ptype == "FIXED_FEE" else 0.0
            eac_cost = fixed_fee_eac.get(pid, 0.0) if ptype == "FIXED_FEE" else cum_cost

            rows.append({
                "project_id": pid,
                "period": period,
                "cost_centre_id": cc_id,
                "client_id": client_id,
                "project_type": ptype,
                "recognition_method": recognition_method,
                "currency": "EUR",
                "hours_worked": round(hours_worked, 2),
                "hours_billable": round(hours_billable, 2),
                "revenue_recognised_eur": round(revenue_recog, 2),
                "cost_recognised_eur": round(cost_recog, 2),
                "margin_recognised_eur": round(revenue_recog - cost_recog, 2),
                "amount_billed_eur": round(amount_billed, 2),
                "contract_value_eur": round(contract_effective, 2),
                "etc_cost_eur": round(etc_cost, 2),
                "eac_cost_eur": round(eac_cost, 2),
                "percent_complete": round(pct_complete, 4),
                "cum_revenue_recognised_eur": round(cum_revenue, 2),
                "cum_cost_recognised_eur": round(cum_cost, 2),
                "cum_billed_eur": round(cum_billed, 2),
                "wip_balance_eur": round(wip_balance, 2),
                "created_at": now,
            })

    return rows


def revenue_dict_from_subledger(subledger):
    """Compatibility helper: collapse subledger to {(project_id, period): revenue}."""
    out: dict[tuple, float] = {}
    for r in subledger:
        if r["revenue_recognised_eur"] > 0:
            out[(r["project_id"], r["period"])] = r["revenue_recognised_eur"]
    return out


def billing_dict_from_subledger(subledger):
    """Compatibility helper: collapse subledger to {period: total_billed}."""
    out: dict[str, float] = {}
    for r in subledger:
        if r["amount_billed_eur"] > 0:
            out[r["period"]] = out.get(r["period"], 0.0) + r["amount_billed_eur"]
    return out


# ──────────────────────────────────────────────────────────────────────────────
# STEP 9 — Journal Entries
# ──────────────────────────────────────────────────────────────────────────────

def _make_entry(je_id, entry_date, period, entry_type, description, lines):
    """
    lines: list of (account_code, cost_centre_id, project_id, debit, credit, desc)
    Returns (journal_entry dict, [journal_line dict, ...])
    """
    entry = {
        "journal_entry_id": je_id,
        "entry_date": entry_date,
        "period": period,
        "entry_type": entry_type,
        "description": description,
        "source": "GENERATED",
        "entity_id": ENTITY_ID,
        "created_at": datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None),
    }
    je_lines = []
    for (acct, cc, proj, dr, cr, ldesc) in lines:
        je_lines.append({
            "journal_line_id": None,  # assigned later
            "journal_entry_id": je_id,
            "account_code": acct,
            "cost_centre_id": cc,
            "project_id": proj,
            "debit_amount": round(dr, 2),
            "credit_amount": round(cr, 2),
            "amount": round(dr - cr, 2),
            "currency": "EUR",
            "description": ldesc,
        })
    return entry, je_lines


def generate_journal_entries(projects, employees, timesheets, payroll, subledger):
    entries = []
    lines_all = []
    je_id = 1
    jl_id = 1

    emp_map = {e["employee_id"]: e for e in employees}

    # Project×period revenue and total billings derived from the subledger.
    # Revenue feeds JE posting (memo ledger reconciles 1:1 against the GL);
    # billings drive AR cash collection.
    revenue = revenue_dict_from_subledger(subledger)
    billings_by_period = billing_dict_from_subledger(subledger)

    # Track AP balances for payment entries
    prev_ap: dict[str, float] = {}       # period -> total AP booked

    for year, month in months_in_range(ACTUALS_START, ACTUALS_END):
        period = period_str(year, month)
        last_bday = last_business_day(year, month)

        # ── 9a: Revenue Recognition ──────────────────────────────────────────
        for proj in projects:
            pid = proj["project_id"]
            rev = revenue.get((pid, period), 0.0)
            if rev <= 0:
                continue

            ptype = proj["project_type"]
            if ptype == "T&M":
                acct_rev = "4100"
            elif ptype == "FIXED_FEE":
                acct_rev = "4200"
            else:
                acct_rev = "4300"

            cc = proj["cost_centre_id"]
            desc = f"{ptype} Revenue - {proj['project_code']} - {period}"
            entry, jl = _make_entry(
                je_id, last_bday, period, "REVENUE", desc,
                [
                    ("1200", cc, pid, rev, 0, desc),
                    (acct_rev, cc, pid, 0, rev, desc),
                ]
            )
            entries.append(entry)
            lines_all.extend(jl)
            je_id += 1

        # ── 9b: Payroll by (cost_centre, employee_type) ──────────────────────
        # employee_type drives the P&L account:
        #   SERVICES → 5100/5110 Direct Labour  (above gross margin)
        #   SUPPORT  → 7800/7810 Admin Salaries (SG&A, below gross margin)
        cc_payroll: dict[tuple, dict] = {}
        for pr in payroll:
            if pr["period"] != period:
                continue
            emp = emp_map.get(pr["employee_id"])
            if not emp:
                continue
            key = (pr["cost_centre_id"], emp["employee_type"])
            if key not in cc_payroll:
                cc_payroll[key] = {"gross": 0.0, "contributions": 0.0, "bonus": 0.0}
            cc_payroll[key]["gross"] += pr["gross_salary_eur"]
            cc_payroll[key]["contributions"] += pr["employer_contributions_eur"]
            cc_payroll[key]["bonus"] += pr["bonus_eur"]

        # Get utilisation per employee for bench cost split rule
        emp_util: dict[int, float] = {}
        for ts in timesheets:
            if ts["period"] != period:
                continue
            eid = ts["employee_id"]
            if ts["hours_billable"] > 0:
                emp_util[eid] = emp_util.get(eid, 0.0) + ts["hours_billable"]

        # Available hours for each emp this period
        bdays = business_days_in_month(year, month)
        hfactor = holiday_factor(month)
        avail = bdays * 8.0 * hfactor

        for (cc, emp_type), amounts in cc_payroll.items():
            gross = amounts["gross"]
            contr = amounts["contributions"]
            bonus = amounts["bonus"]

            # Account selection driven purely by employee_type
            if emp_type == "SERVICES":
                sal_acct = "5100"
                con_acct = "5110"
            else:  # SUPPORT
                sal_acct = "7800"
                con_acct = "7810"

            # Bench cost split (SERVICES only): employees with < 20% utilisation
            # → reclassify 50% of their cost from Direct Labour to Indirect Labour
            bench_gross = 0.0
            if emp_type == "SERVICES":
                for pr in payroll:
                    if pr["period"] != period or pr["cost_centre_id"] != cc:
                        continue
                    pr_emp = emp_map.get(pr["employee_id"])
                    if not pr_emp or pr_emp["employee_type"] != "SERVICES":
                        continue
                    util_pct = emp_util.get(pr["employee_id"], 0.0) / avail if avail > 0 else 0.0
                    if util_pct < 0.20:
                        bench_gross += pr["gross_salary_eur"] * 0.50

            desc = f"Payroll - {cc} - {emp_type} - {period}"
            lines = [
                (sal_acct, cc, None, gross, 0, desc),
                (con_acct, cc, None, contr, 0, desc),
                ("2300", cc, None, 0, gross + contr, desc),
            ]

            # Bench reclassification: CR 5100/5110, DR 6100/6110
            if bench_gross > 0:
                bench_contr_amt = bench_gross * 0.30
                lines += [
                    ("5100", cc, None, 0, bench_gross, f"Bench reclass - {cc} - {period}"),
                    ("5110", cc, None, 0, bench_contr_amt, f"Bench reclass - {cc} - {period}"),
                    ("6100", cc, None, bench_gross, 0, f"Bench reclass - {cc} - {period}"),
                    ("6110", cc, None, bench_contr_amt, 0, f"Bench reclass - {cc} - {period}"),
                ]

            if gross + contr > 0:
                entry, jl = _make_entry(je_id, last_bday, period, "PAYROLL", desc, lines)
                entries.append(entry)
                lines_all.extend(jl)
                je_id += 1

            # Bonus entry (December)
            if bonus > 0:
                bdesc = f"Bonus Accrual - {cc} - {emp_type} - {period}"
                b_sal_acct = sal_acct  # same account as regular payroll for this type
                entry, jl = _make_entry(
                    je_id, last_bday, period, "BONUS", bdesc,
                    [
                        (b_sal_acct, cc, None, bonus, 0, bdesc),
                        ("2200", cc, None, 0, bonus, bdesc),
                    ]
                )
                entries.append(entry)
                lines_all.extend(jl)
                je_id += 1

        # ── 9c: Indirect / SGA Costs ─────────────────────────────────────────
        n_hires_this_month = sum(
            1 for e in employees
            if e["start_date"].year == year and e["start_date"].month == month
        )
        n_active_projects = sum(
            1 for p in projects
            if p["start_date"] <= last_bday
            and (p["end_date"] is None or p["end_date"] >= datetime.date(year, month, 1))
        )

        # Active headcount this month (drives per-seat / per-head costs)
        month_start_dt = datetime.date(year, month, 1)
        active_hc = sum(
            1 for e in employees
            if e["start_date"] <= last_bday
            and (e["end_date"] is None or e["end_date"] >= month_start_dt)
        )
        # Revenue run-rate index relative to 2023 base (for revenue-linked costs)
        rev_annual_target = {2022: 13_000_000, 2023: 15_000_000,
                             2024: 17_000_000, 2025: 19_000_000, 2026: 21_000_000}
        rev_index = rev_annual_target.get(year, 15_000_000) / 15_000_000

        # ── Facilities ────────────────────────────────────────────────────────
        # Rent: step-function capacity model — grows when headcount crosses bands.
        # Base EUR 55k/month for ≤110 hc; +EUR 12k for each additional band of 15.
        rent_bands = max(0, (active_hc - 110) // 15)
        rent_base = 55_000.0 + rent_bands * 12_000
        rent_annual_escalation = 1 + 0.025 * (year - 2023)  # 2.5%/yr CPI uplift
        rent = round(rent_base * rent_annual_escalation, 2)

        # ── Technology ───────────────────────────────────────────────────────
        # Fixed platform cost + per-seat licence cost (tools, SaaS, cloud workspaces)
        # EUR 180/employee/month for per-seat tooling (Jira, Confluence, O365, etc.)
        # Annual renewal spike in January.
        it_per_seat = 180 * active_hc
        it_platform = 12_000.0  # fixed infrastructure baseline
        it_noise = np.random.normal(0, 0.07 * (it_per_seat + it_platform))
        it_jan_spike = 15_000 if month == 1 else 0  # annual software renewals
        it = max(it_platform, it_per_seat + it_platform + it_noise + it_jan_spike)

        # ── Sales & Marketing ─────────────────────────────────────────────────
        # BD/marketing spend modelled as ~2.5% of annualised revenue run-rate,
        # distributed unevenly: peaks in Q1 (new FY push) and Q3 (H2 pipeline).
        mkt_annual_budget = rev_annual_target.get(year, 15_000_000) * 0.025
        mkt_monthly_base = mkt_annual_budget / 12
        mkt_seasonal = 1.4 if month in [1, 2, 3] else (1.2 if month in [7, 8, 9] else 0.8)
        mkt = mkt_monthly_base * mkt_seasonal + np.random.normal(0, mkt_monthly_base * 0.1)
        mkt = max(8_000, mkt)

        # ── Legal & Professional Services ────────────────────────────────────
        # Right-skewed (lognormal); Q1 spike for annual audit + statutory filings.
        # Grows modestly with revenue (more complex contracts as business scales).
        legal_base = 8_000 * rev_index
        legal_mu = math.log(legal_base) - 0.5
        legal = rng_lognormal(legal_mu, 0.6) * (2.2 if month in [1, 2, 3] else 1.0)
        legal = max(6_000, min(legal, 50_000 * rev_index))

        # ── Travel & Entertainment ────────────────────────────────────────────
        # Scales with billable headcount: consultants travel to client sites.
        # ~EUR 300/billable_employee/month average; low in Aug and Dec.
        billable_hc = sum(
            1 for e in employees
            if e["grade"] != "ADMIN"
            and e["start_date"] <= last_bday
            and (e["end_date"] is None or e["end_date"] >= month_start_dt)
        )
        te_base = billable_hc * 300
        te_seasonal = 0.55 if month in [8, 12] else (1.25 if month in [1, 2, 3, 7, 9] else 1.0)
        te = max(15_000, np.random.normal(te_base, te_base * 0.15) * te_seasonal)

        depr = rng_uniform(8_000, 8_500)
        ins = rng_uniform(4_000, 4_200)
        bank = rng_uniform(500, 800)
        interest = rng_uniform(200, 500)

        # ── Subcontractors ───────────────────────────────────────────────────
        # 5-8% of revenue; scales with actual monthly revenue recognised.
        period_total_rev = sum(v for (_, p_), v in revenue.items() if p_ == period)
        rev_scale = period_total_rev / (17_000_000 / 12) if period_total_rev else 1.0
        sub = max(50_000, np.random.normal(90_000, 22_000) * rev_scale)

        # ── Indirect people costs ─────────────────────────────────────────────
        # Training: EUR 120/employee/month; peaks in L&D seasons (Q1, Q3).
        training_per_head = 120 * active_hc
        training = training_per_head * (1.4 if month in [1, 2, 3, 7, 8, 9] else 0.8)
        training = max(5_000, training + np.random.normal(0, training * 0.1))

        recruit = n_hires_this_month * 5_000 + rng_uniform(3_000, 5_000)
        proj_exp = n_active_projects * 500 + rng_uniform(3_000, 5_000)

        # Use AP for 70%, Cash for 30%
        def ap_or_cash():
            return "2100" if random.random() < 0.70 else "1100"

        sga_items = [
            ("7100", "CC-GADM-01", None, rent,     "Office Rent"),
            ("7200", "CC-GADM-01", None, it,       "IT & Software Licences"),
            ("7300", "CC-MKTG-01", None, mkt,      "Marketing & Events"),
            ("7400", "CC-FINC-01", None, legal,    "Legal & Audit"),
            ("7500", "CC-GADM-01", None, te,       "Travel & Entertainment"),
            ("7600", "CC-GADM-01", None, depr,     "Depreciation"),
            ("7700", "CC-GADM-01", None, ins,      "Insurance"),
            ("8200", "CC-FINC-01", None, bank,     "Bank Charges"),
            ("5200", "CC-GADM-01", None, sub,      "Subcontractor Costs"),
            ("6200", "CC-HRES-01", None, training, "Training & Development"),
            ("6300", "CC-HRES-01", None, recruit,  "Recruitment Costs"),
            ("5300", "CC-GADM-01", None, proj_exp, "Direct Project Expenses"),
        ]

        total_ap = 0.0
        for acct, cc, proj, amount, label in sga_items:
            amount = round(amount, 2)
            offset_acct = ap_or_cash()
            if offset_acct == "2100":
                total_ap += amount
            desc = f"{label} - {period}"
            entry, jl = _make_entry(
                je_id, last_bday, period, "EXPENSE", desc,
                [
                    (acct, cc, proj, amount, 0, desc),
                    (offset_acct, cc, proj, 0, amount, desc),
                ]
            )
            entries.append(entry)
            lines_all.extend(jl)
            je_id += 1

        # Interest income (credit revenue side)
        interest = round(interest, 2)
        desc = f"Interest Income - {period}"
        entry, jl = _make_entry(
            je_id, last_bday, period, "EXPENSE", desc,
            [
                ("1100", "CC-FINC-01", None, interest, 0, desc),
                ("8100", "CC-FINC-01", None, 0, interest, desc),
            ]
        )
        entries.append(entry)
        lines_all.extend(jl)
        je_id += 1

        prev_ap[period] = total_ap

        # ── 9d: Cash Collection (prior period billings) ──────────────────────
        # Cash collection follows BILLINGS, not revenue recognition. The
        # subledger drives the cadence (T&M monthly, fixed-fee 30/40/30,
        # milestone on event) — collection lags by ~1 month with 85-95%
        # collection rate to model realistic AR aging.
        if month > 1:
            prev_p = period_str(year, month - 1)
        else:
            prev_p = period_str(year - 1, 12)
        prior_billings = billings_by_period.get(prev_p, 0.0)
        if prior_billings > 0:
            collect_rate = rng_uniform(0.85, 0.95)
            collected = round(prior_billings * collect_rate, 2)
            desc = f"Cash Collection - {period}"
            cc_col = "CC-FINC-01"
            entry, jl = _make_entry(
                je_id, last_bday, period, "CASH_COLLECTION", desc,
                [
                    ("1100", cc_col, None, collected, 0, desc),
                    ("1200", cc_col, None, 0, collected, desc),
                ]
            )
            entries.append(entry)
            lines_all.extend(jl)
            je_id += 1

        # ── 9e: AP Payment (prior month AP) ──────────────────────────────────
        prior_ap = prev_ap.get(prev_p, 0.0)
        if prior_ap > 0:
            pay_rate = rng_uniform(0.90, 0.98)
            paid = round(prior_ap * pay_rate, 2)
            desc = f"AP Payment - {period}"
            entry, jl = _make_entry(
                je_id, last_bday, period, "AP_PAYMENT", desc,
                [
                    ("2100", "CC-FINC-01", None, paid, 0, desc),
                    ("1100", "CC-FINC-01", None, 0, paid, desc),
                ]
            )
            entries.append(entry)
            lines_all.extend(jl)
            je_id += 1

    # Assign sequential journal_line_ids
    for jl in lines_all:
        jl["journal_line_id"] = jl_id
        jl_id += 1

    return entries, lines_all


# ──────────────────────────────────────────────────────────────────────────────
# STEP 9 validation — double-entry check
# ──────────────────────────────────────────────────────────────────────────────

def validate_journal_entries(entries, lines_all):
    from collections import defaultdict
    totals: dict[int, list] = defaultdict(lambda: [0.0, 0.0])
    for jl in lines_all:
        je_id = jl["journal_entry_id"]
        totals[je_id][0] += jl["debit_amount"]
        totals[je_id][1] += jl["credit_amount"]

    errors = []
    for je_id, (dr, cr) in totals.items():
        if abs(dr - cr) > 0.01:
            errors.append(f"JE {je_id}: debit={dr:.2f} credit={cr:.2f} diff={dr-cr:.2f}")

    if errors:
        print(f"[VALIDATION ERROR] {len(errors)} unbalanced journal entries!")
        for e in errors[:10]:
            print(f"  {e}")
        raise ValueError("Journal entries do not balance — fix generation logic.")
    print(f"[OK] All {len(entries)} journal entries balance.")


# ──────────────────────────────────────────────────────────────────────────────
# STEP 10 & 11 — Budget and Forecast Scenarios
# ──────────────────────────────────────────────────────────────────────────────

def generate_budget_entries(journal_lines, journal_entries, employees):
    """
    BUD-2026: based on 2025 actuals, +12% growth + 4% optimism for revenue,
    +5% inflation × 0.97 cost control for costs.
    Statistical accounts derived from Dec 2025 headcount.
    Returns list of planning entry dicts.
    """
    acct_map_local = {a[0]: a for a in ACCOUNTS}

    # Aggregate 2025 actuals by (account_code, cost_centre_id)
    je_period = {e["journal_entry_id"]: e["period"] for e in journal_entries}
    actuals_2025: dict[tuple, float] = {}

    for jl in journal_lines:
        period = je_period.get(jl["journal_entry_id"], "")
        if not period.startswith("2025-"):
            continue
        acct = jl["account_code"]
        acct_info = acct_map_local.get(acct)
        if not acct_info:
            continue
        atype = acct_info[2]
        if atype in ("HEADER", "ASSET", "LIABILITY", "EQUITY"):
            continue  # P&L accounts only
        key = (acct, jl["cost_centre_id"])
        # Use signed amount for aggregation
        actuals_2025[key] = actuals_2025.get(key, 0.0) + jl["amount"]

    entries = []
    for (acct, cc), total_2025 in actuals_2025.items():
        if abs(total_2025) < 1.0:
            continue
        acct_info = acct_map_local.get(acct)
        if not acct_info:
            continue
        atype = acct_info[2]

        # Spread evenly across 12 months, with minor seasonal smoothing
        if atype in ("REVENUE", "OTHER_INCOME"):
            monthly_amt = (total_2025 * 1.12 * 1.04) / 12
            # Revenue is credit → negative amount in journal; budget uses positive
            monthly_amt = abs(monthly_amt)
            for m in range(1, 13):
                s = SEASONAL_MULTIPLIER[m]
                amt = round((monthly_amt * s / sum(SEASONAL_MULTIPLIER.values()) * 12), 2)
                if abs(amt) < 1:
                    continue
                entries.append({
                    "account": acct,
                    "cost_centre": cc,
                    "period": period_str(BUDGET_YEAR, m),
                    "scenario": "BUD-2026",
                    "delta_amount": -round(amt, 2),  # revenue = credit = negative
                    "user_id": "system",
                    "inserted_at": datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None),
                })
        else:
            # Cost accounts
            monthly_base = abs(total_2025) * 1.05 * 0.97 / 12
            for m in range(1, 13):
                amt = round(monthly_base, 2)
                if abs(amt) < 1:
                    continue
                entries.append({
                    "account": acct,
                    "cost_centre": cc,
                    "period": period_str(BUDGET_YEAR, m),
                    "scenario": "BUD-2026",
                    "delta_amount": round(amt, 2),  # costs = debit = positive
                    "user_id": "system",
                    "inserted_at": datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None),
                })

    # ── Statistical accounts: derived from Dec 2025 headcount ──────────────
    # Count active employees per cost centre in Dec 2025
    dec_start = datetime.date(2025, 12, 1)
    dec_end = datetime.date(2025, 12, 31)
    billable_fte: dict[str, int] = {}
    overhead_fte: dict[str, int] = {}

    for emp in employees:
        emp_start = emp["start_date"]
        emp_end = emp["end_date"]  # None = still active
        if emp_start <= dec_end and (emp_end is None or emp_end >= dec_start):
            cc_id = emp["cost_centre_id"]
            cc_info = CC_MAP.get(cc_id)
            if cc_info is None:
                continue
            if cc_info[4]:  # is_billable
                billable_fte[cc_id] = billable_fte.get(cc_id, 0) + 1
            else:
                overhead_fte[cc_id] = overhead_fte.get(cc_id, 0) + 1

    FIXED_WORK_DAYS = 21.5
    HOURS_PER_DAY = 8.0
    BILLABLE_UTIL_TARGET = 0.75
    now_ts = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

    for cc_id, fte_count in billable_fte.items():
        for m in range(1, 13):
            billable_hrs = round(fte_count * FIXED_WORK_DAYS * HOURS_PER_DAY * BILLABLE_UTIL_TARGET, 2)
            total_hrs = round(fte_count * FIXED_WORK_DAYS * HOURS_PER_DAY, 2)
            for acct_code, val in [("9100", billable_hrs), ("9110", total_hrs), ("9200", float(fte_count))]:
                entries.append({
                    "account": acct_code,
                    "cost_centre": cc_id,
                    "period": period_str(BUDGET_YEAR, m),
                    "scenario": "BUD-2026",
                    "delta_amount": val,
                    "user_id": "system",
                    "inserted_at": now_ts,
                })

    for cc_id, fte_count in overhead_fte.items():
        for m in range(1, 13):
            total_hrs = round(fte_count * FIXED_WORK_DAYS * HOURS_PER_DAY, 2)
            for acct_code, val in [("9110", total_hrs), ("9210", float(fte_count))]:
                entries.append({
                    "account": acct_code,
                    "cost_centre": cc_id,
                    "period": period_str(BUDGET_YEAR, m),
                    "scenario": "BUD-2026",
                    "delta_amount": val,
                    "user_id": "system",
                    "inserted_at": now_ts,
                })

    return entries


def generate_forecast_entries(budget_entries):
    """
    FC-2026-Q1: Copy of BUD-2026 + adjustment deltas.
    """
    forecast = []
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

    # Step 1: copy all budget rows
    for be in budget_entries:
        forecast.append({
            "account": be["account"],
            "cost_centre": be["cost_centre"],
            "period": be["period"],
            "scenario": "FC-2026-Q1",
            "delta_amount": be["delta_amount"],
            "user_id": "system",
            "inserted_at": now,
        })

    # Step 2: adjustment deltas
    acct_map_local = {a[0]: a for a in ACCOUNTS}
    for be in budget_entries:
        acct_info = acct_map_local.get(be["account"])
        if not acct_info:
            continue
        atype = acct_info[2]
        acct_code = be["account"]

        if atype == "REVENUE":
            adj_pct = rng_uniform(-0.03, 0.01)
            adj = round(be["delta_amount"] * adj_pct, 2)
        elif atype == "STATISTICAL":
            if acct_code == "9100":
                # Billable Hours: -2% to +1% adjustment
                adj_pct = rng_uniform(-0.02, 0.01)
                adj = round(be["delta_amount"] * adj_pct, 2)
            elif acct_code == "9200":
                # FTEs - Billable: -1 to +1 absolute FTE delta
                adj = round(rng_uniform(-1.0, 1.0), 2)
            else:
                # 9110 Total Hours and 9210 FTEs - Overhead: no adjustment
                continue
        else:
            adj_pct = rng_uniform(0.01, 0.04)
            adj = round(be["delta_amount"] * adj_pct, 2)

        if abs(adj) < 0.01:
            continue
        forecast.append({
            "account": acct_code,
            "cost_centre": be["cost_centre"],
            "period": be["period"],
            "scenario": "FC-2026-Q1",
            "delta_amount": adj,
            "user_id": "system",
            "inserted_at": now,
        })

    return forecast


# ──────────────────────────────────────────────────────────────────────────────
# DDL helpers
# ──────────────────────────────────────────────────────────────────────────────

PG_DDL = """
CREATE SCHEMA IF NOT EXISTS gl;
CREATE SCHEMA IF NOT EXISTS hr;
CREATE SCHEMA IF NOT EXISTS projects;
CREATE SCHEMA IF NOT EXISTS finance;

-- gl.accounts
DROP TABLE IF EXISTS gl.accounts CASCADE;
CREATE TABLE gl.accounts (
    account_code   VARCHAR(4)   PRIMARY KEY,
    account_name   VARCHAR(100) NOT NULL,
    account_type   VARCHAR(20)  NOT NULL,
    fs_line        VARCHAR(20)  NOT NULL,
    normal_balance VARCHAR(2)   NOT NULL,
    parent_code    VARCHAR(4)   REFERENCES gl.accounts(account_code),
    is_active      BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMP    NOT NULL
);

-- master.cost_centres
DROP TABLE IF EXISTS master.cost_centres CASCADE;
CREATE SCHEMA IF NOT EXISTS master;
CREATE TABLE master.cost_centres (
    cost_centre_id   VARCHAR(12)  PRIMARY KEY,
    cost_centre_name VARCHAR(100) NOT NULL,
    department       VARCHAR(60)  NOT NULL,
    division         VARCHAR(60)  NOT NULL,
    entity_id        VARCHAR(10)  NOT NULL DEFAULT 'ENT-001',
    is_billable      BOOLEAN      NOT NULL,
    created_at       TIMESTAMP    NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMP    NOT NULL DEFAULT NOW()
);

-- hr.employees
DROP TABLE IF EXISTS hr.timesheets CASCADE;
DROP TABLE IF EXISTS hr.payroll CASCADE;
DROP TABLE IF EXISTS hr.employees CASCADE;
CREATE TABLE hr.employees (
    employee_id          SERIAL       PRIMARY KEY,
    employee_code        VARCHAR(10)  UNIQUE NOT NULL,
    first_name           VARCHAR(50),
    last_name            VARCHAR(50),
    email                VARCHAR(100),
    grade                VARCHAR(5),
    employee_type        VARCHAR(8),
    role_title           VARCHAR(80),
    cost_centre_id       VARCHAR(12),
    annual_cost          DECIMAL(10,2),
    daily_bill_rate      DECIMAL(8,2),
    fte                  DECIMAL(3,2),
    start_date           DATE,
    end_date             DATE,
    is_active            BOOLEAN,
    currency             VARCHAR(3),
    created_at           TIMESTAMP,
    updated_at           TIMESTAMP
);

-- projects.clients (master data)
DROP TABLE IF EXISTS projects.clients CASCADE;
CREATE TABLE projects.clients (
    client_id    VARCHAR(10)  PRIMARY KEY,
    client_name  VARCHAR(120) NOT NULL,
    industry     VARCHAR(60),
    tier         VARCHAR(10),
    country      VARCHAR(3),
    created_date DATE         NOT NULL
);

-- projects.projects
DROP TABLE IF EXISTS projects.projects CASCADE;
CREATE TABLE projects.projects (
    project_id          SERIAL       PRIMARY KEY,
    project_code        VARCHAR(12)  UNIQUE NOT NULL,
    project_name        VARCHAR(120),
    client_id           VARCHAR(10),
    client_name         VARCHAR(100),
    project_type        VARCHAR(15),
    status              VARCHAR(15),
    start_date          DATE,
    end_date            DATE,
    budget_hours        DECIMAL(10,1),
    budget_revenue      DECIMAL(12,2),
    contract_value      DECIMAL(12,2),
    project_manager_id  INTEGER      REFERENCES hr.employees(employee_id),
    cost_centre_id      VARCHAR(12),
    currency            VARCHAR(3),
    created_at          TIMESTAMP,
    updated_at          TIMESTAMP
);

-- hr.timesheets
CREATE TABLE hr.timesheets (
    timesheet_id      SERIAL      PRIMARY KEY,
    employee_id       INTEGER     REFERENCES hr.employees(employee_id),
    project_id        INTEGER     REFERENCES projects.projects(project_id),
    cost_centre_id    VARCHAR(12),
    period_start_date DATE        NOT NULL,
    hours_worked      DECIMAL(6,2),
    hours_billable    DECIMAL(6,2),
    activity_type     VARCHAR(20),
    created_at        TIMESTAMP
);

-- hr.payroll
CREATE TABLE hr.payroll (
    payroll_id                   SERIAL      PRIMARY KEY,
    employee_id                  INTEGER     REFERENCES hr.employees(employee_id),
    cost_centre_id               VARCHAR(12),
    pay_date                     DATE        NOT NULL,
    gross_salary                 DECIMAL(10,2),
    employer_contributions       DECIMAL(10,2),
    bonus                        DECIMAL(10,2),
    total_cost                   DECIMAL(10,2),
    currency                     VARCHAR(3),
    created_at                   TIMESTAMP
);

-- projects.revenue_subledger (memo subledger to GL revenue)
-- Grain: one row per (project_id, period). Reconciles to GL revenue at the
-- project×period grain on the revenue side, and to GL direct cost at the
-- CC×period grain on the cost side (subledger captures only billable
-- project effort, not bench / indirect / SG&A).
DROP TABLE IF EXISTS projects.revenue_subledger CASCADE;
CREATE TABLE projects.revenue_subledger (
    subledger_id              SERIAL       PRIMARY KEY,
    project_id                INTEGER      NOT NULL REFERENCES projects.projects(project_id),
    recognition_date          DATE         NOT NULL,
    cost_centre_id            VARCHAR(12)  NOT NULL,
    client_id                 VARCHAR(10)  NOT NULL REFERENCES projects.clients(client_id),
    project_type              VARCHAR(15)  NOT NULL,
    recognition_method        VARCHAR(15)  NOT NULL,
    currency                  VARCHAR(3)   NOT NULL,
    -- effort
    hours_worked              DECIMAL(10,2) NOT NULL DEFAULT 0,
    hours_billable            DECIMAL(10,2) NOT NULL DEFAULT 0,
    -- period flow
    revenue_recognised        DECIMAL(14,2) NOT NULL DEFAULT 0,
    cost_recognised           DECIMAL(14,2) NOT NULL DEFAULT 0,
    amount_billed             DECIMAL(14,2) NOT NULL DEFAULT 0,
    -- contract / progress
    contract_value            DECIMAL(14,2) NOT NULL DEFAULT 0,
    percent_complete          DECIMAL(5,4)  NOT NULL DEFAULT 0,
    -- cumulative stock (as of period end)
    cum_revenue_recognised    DECIMAL(14,2) NOT NULL DEFAULT 0,
    cum_cost_recognised       DECIMAL(14,2) NOT NULL DEFAULT 0,
    cum_billed                DECIMAL(14,2) NOT NULL DEFAULT 0,
    -- derived stock: positive = unbilled WIP (asset),
    --                negative = deferred revenue (liability)
    wip_balance               DECIMAL(14,2) NOT NULL DEFAULT 0,
    created_at                TIMESTAMP    NOT NULL,
    UNIQUE (project_id, recognition_date)
);
CREATE INDEX idx_subledger_date         ON projects.revenue_subledger (recognition_date);
CREATE INDEX idx_subledger_client       ON projects.revenue_subledger (client_id);
CREATE INDEX idx_subledger_cost_centre  ON projects.revenue_subledger (cost_centre_id);

-- gl.journal_entries
DROP TABLE IF EXISTS gl.journal_lines CASCADE;
DROP TABLE IF EXISTS gl.journal_entries CASCADE;
CREATE TABLE gl.journal_entries (
    journal_entry_id SERIAL       PRIMARY KEY,
    entry_date       DATE,
    period           VARCHAR(7),
    entry_type       VARCHAR(20),
    description      VARCHAR(200),
    source           VARCHAR(20),
    entity_id        VARCHAR(10),
    created_at       TIMESTAMP
);

-- gl.journal_lines
CREATE TABLE gl.journal_lines (
    journal_line_id  SERIAL       PRIMARY KEY,
    journal_entry_id INTEGER      REFERENCES gl.journal_entries(journal_entry_id),
    account_code     VARCHAR(4)   REFERENCES gl.accounts(account_code),
    cost_centre_id   VARCHAR(12),
    project_id       INTEGER,
    debit_amount     DECIMAL(12,2),
    credit_amount    DECIMAL(12,2),
    amount           DECIMAL(12,2),
    currency         VARCHAR(3),
    description      VARCHAR(200)
);

-- gl.journal_postings
-- Source-style view exposed to the ingestion path. Joins normalised
-- journal_entries + journal_lines and projects exactly the columns the
-- customer_pg__gl binding lands: accounting `period` (String 'YYYY-MM',
-- carried directly off journal_entries so adjustment / 13th periods
-- pass through unchanged), `account_code`, `cost_centre_id`, `amount`.
-- The view sits in the `gl.*` schema (operational source-system shape)
-- rather than `finance.*` (denormalised reporting shape for the federated
-- inspection tool); the two paths are kept distinct on purpose.
DROP VIEW IF EXISTS gl.journal_postings CASCADE;
CREATE VIEW gl.journal_postings AS
SELECT
    e.period         AS period,
    jl.account_code  AS account_code,
    jl.cost_centre_id AS cost_centre_id,
    jl.amount        AS amount
FROM gl.journal_lines jl
JOIN gl.journal_entries e ON e.journal_entry_id = jl.journal_entry_id;

-- finance.gl_transactions_detail
-- Postgres-only federated source: denormalised posting-level transaction data
-- similar to what a client warehouse or ERP reporting replica would expose.
DROP VIEW IF EXISTS finance.gl_metric_view CASCADE;
DROP TABLE IF EXISTS finance.gl_transactions_detail CASCADE;
CREATE TABLE finance.gl_transactions_detail (
    transaction_id    VARCHAR(40)  PRIMARY KEY,
    journal_entry_id  INTEGER      NOT NULL,
    journal_line_id   INTEGER      NOT NULL,
    scenario          VARCHAR(20)  NOT NULL DEFAULT 'ACTUALS',
    entity_id         VARCHAR(10)  NOT NULL,
    period            VARCHAR(7)   NOT NULL,
    posting_date      DATE         NOT NULL,
    document_ref      VARCHAR(30),
    document_type     VARCHAR(20),
    supplier_id       VARCHAR(20),
    supplier_name     VARCHAR(120),
    account_code      VARCHAR(4)   NOT NULL,
    account_name      VARCHAR(100) NOT NULL,
    account_type      VARCHAR(20)  NOT NULL,
    fs_line           VARCHAR(20)  NOT NULL,
    cost_centre_id    VARCHAR(12),
    project_id        INTEGER,
    debit_amount      DECIMAL(14,2) NOT NULL,
    credit_amount     DECIMAL(14,2) NOT NULL,
    amount            DECIMAL(14,2) NOT NULL,
    debit_credit      VARCHAR(1)   NOT NULL,
    currency          VARCHAR(3)   NOT NULL,
    description       VARCHAR(240),
    source_system     VARCHAR(20)  NOT NULL,
    posted_by         VARCHAR(80),
    approval_status   VARCHAR(20),
    created_at        TIMESTAMP    NOT NULL
);
CREATE INDEX idx_finance_gl_detail_period ON finance.gl_transactions_detail (period);
CREATE INDEX idx_finance_gl_detail_cc     ON finance.gl_transactions_detail (cost_centre_id);
CREATE INDEX idx_finance_gl_detail_acct   ON finance.gl_transactions_detail (account_code);
CREATE INDEX idx_finance_gl_detail_fsline ON finance.gl_transactions_detail (fs_line);

CREATE VIEW finance.gl_metric_view AS
SELECT
    gl.transaction_id,
    gl.journal_entry_id,
    gl.journal_line_id,
    gl.scenario,
    gl.entity_id,
    gl.period,
    gl.posting_date,
    gl.document_ref,
    gl.document_type,
    gl.supplier_id,
    gl.supplier_name,
    gl.account_code,
    gl.cost_centre_id AS cost_centre,
    gl.account_code AS account,
    gl.account_name,
    gl.account_type,
    gl.fs_line,
    -- Cost-centre hierarchy denormalised onto the federated view: the engine
    -- cannot join ClickHouse master data to a foreign fact at query time, so
    -- the parents must be present here to be groupable.
    cc.department,
    cc.division,
    gl.project_id,
    gl.debit_amount,
    gl.credit_amount,
    gl.amount,
    gl.currency,
    gl.description,
    gl.source_system,
    gl.posted_by,
    gl.approval_status,
    gl.created_at
FROM finance.gl_transactions_detail gl
LEFT JOIN master.cost_centres cc ON gl.cost_centre_id = cc.cost_centre_id;

-- finance.project_worklog_detail
-- Postgres-only federated source: enriched worklog / time-entry detail.
DROP VIEW IF EXISTS finance.worklog_metric_view CASCADE;
DROP TABLE IF EXISTS finance.project_worklog_detail CASCADE;
CREATE TABLE finance.project_worklog_detail (
    worklog_id         VARCHAR(40)  PRIMARY KEY,
    timesheet_id       INTEGER      NOT NULL,
    scenario           VARCHAR(20)  NOT NULL DEFAULT 'ACTUALS',
    period             VARCHAR(7)   NOT NULL,
    work_date          DATE         NOT NULL,
    employee_id        INTEGER      NOT NULL,
    employee_code      VARCHAR(10),
    employee_name      VARCHAR(120),
    grade              VARCHAR(5),
    project_id         INTEGER,
    project_code       VARCHAR(12),
    project_name       VARCHAR(120),
    client_id          VARCHAR(10),
    client_name        VARCHAR(120),
    cost_centre_id     VARCHAR(12),
    activity_type      VARCHAR(20),
    task_code          VARCHAR(30),
    task_description   VARCHAR(160),
    hours_worked       DECIMAL(8,2) NOT NULL,
    hours_billable     DECIMAL(8,2) NOT NULL,
    daily_bill_rate_eur DECIMAL(10,2),
    billable_amount_eur DECIMAL(14,2) NOT NULL,
    approval_status    VARCHAR(20),
    submitted_at       TIMESTAMP,
    approved_at        TIMESTAMP,
    approved_by        VARCHAR(80),
    source_system      VARCHAR(20)  NOT NULL,
    created_at         TIMESTAMP    NOT NULL
);
CREATE INDEX idx_finance_worklog_period ON finance.project_worklog_detail (period);
CREATE INDEX idx_finance_worklog_cc     ON finance.project_worklog_detail (cost_centre_id);
CREATE INDEX idx_finance_worklog_proj   ON finance.project_worklog_detail (project_id);
CREATE INDEX idx_finance_worklog_emp    ON finance.project_worklog_detail (employee_id);

CREATE VIEW finance.worklog_metric_view AS
SELECT
    w.worklog_id,
    w.timesheet_id,
    w.scenario,
    w.period,
    w.work_date,
    w.employee_id,
    w.employee_code,
    w.employee_name,
    w.grade,
    w.project_id,
    w.project_code,
    w.project_name,
    w.client_id,
    w.client_name,
    w.cost_centre_id AS cost_centre,
    -- Cost-centre hierarchy denormalised onto the federated view (see
    -- gl_metric_view): the engine cannot join master data across backends.
    cc.department,
    cc.division,
    w.activity_type,
    w.task_code,
    w.task_description,
    w.hours_worked,
    w.hours_billable,
    w.daily_bill_rate_eur,
    w.billable_amount_eur,
    w.approval_status,
    w.submitted_at,
    w.approved_at,
    w.approved_by,
    w.source_system,
    w.created_at
FROM finance.project_worklog_detail w
LEFT JOIN master.cost_centres cc ON w.cost_centre_id = cc.cost_centre_id;

-- finance.intercompany_transactions — intra-group recharge memo subledger.
-- Standalone (not tied to GL/plan): a cost centre (cost_centre_id) is recharged
-- `amount` by a counterparty cost centre (counterparty_cc_id) in `period`. Both
-- ids reference master.cost_centres — counterparty is the same master playing a
-- second role, exercised by the counterparty (role-playing) catalogue dimension.
DROP TABLE IF EXISTS finance.intercompany_transactions CASCADE;
CREATE TABLE finance.intercompany_transactions (
    intercompany_id    SERIAL        PRIMARY KEY,
    cost_centre_id     VARCHAR(12)   NOT NULL,
    counterparty_cc_id VARCHAR(12)   NOT NULL,
    period             VARCHAR(7)    NOT NULL,
    amount             DECIMAL(14,2) NOT NULL,
    currency           VARCHAR(3)    NOT NULL DEFAULT 'EUR',
    created_at         TIMESTAMP     NOT NULL
);
CREATE INDEX idx_finance_interco_period ON finance.intercompany_transactions (period);
"""

CH_DDL = """
CREATE DATABASE IF NOT EXISTS gl;
CREATE DATABASE IF NOT EXISTS semantic;

-- semantic.scenarios is created + seeded by scenario_runner.apply() from
-- instance/scenarios.yml (the package owns the table schema; the rows are
-- instance config). Dropped here so a regen starts from a clean registry;
-- the runner re-creates it via CREATE TABLE IF NOT EXISTS.
DROP TABLE IF EXISTS semantic.scenarios;

DROP TABLE IF EXISTS gl.dim_period;
CREATE TABLE gl.dim_period (
    period       String,
    quarter      String,
    fiscal_year  String
) ENGINE = ReplacingMergeTree()
ORDER BY (period);

DROP TABLE IF EXISTS gl.dim_account;
CREATE TABLE gl.dim_account (
    account_code   String,
    account_name   String,
    account_type   String,
    fs_line        String,
    normal_balance String,
    parent_code    Nullable(String),
    is_active      Bool,
    created_at     DateTime
) ENGINE = ReplacingMergeTree()
ORDER BY (account_code);

DROP TABLE IF EXISTS gl.dim_cost_centre;
CREATE TABLE gl.dim_cost_centre (
    cost_centre_id   String,
    cost_centre_name String,
    department       String,
    division         String,
    entity_id        String,
    is_billable      Bool,
    updated_at       DateTime
) ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (cost_centre_id);

"""


SUPPLIER_BY_FS_LINE = {
    "DirectCost": [
        ("SUP-1001", "Nexora Contractor Services GmbH"),
        ("SUP-1002", "BluePeak Delivery Partners GmbH"),
        ("SUP-1003", "Keller Cloud Specialists GmbH"),
        ("SUP-1004", "Vector9 Engineering Freelance GmbH"),
    ],
    "Indirect": [
        ("SUP-2001", "CloudGrid Hosting GmbH"),
        ("SUP-2002", "Atlas Software Subscriptions GmbH"),
        ("SUP-2003", "Northstar Facilities GmbH"),
        ("SUP-2004", "Rhine Travel Services GmbH"),
    ],
    "SGA": [
        ("SUP-3001", "Hanse Legal Advisors GmbH"),
        ("SUP-3002", "Mitte Audit Partners GmbH"),
        ("SUP-3003", "TalentWorks Recruiting GmbH"),
        ("SUP-3004", "MarketForge Events GmbH"),
    ],
    "Other": [
        ("SUP-4001", "Continental Insurance Services GmbH"),
        ("SUP-4002", "EuroBank Fees & Treasury GmbH"),
    ],
}

POSTED_BY_USERS = [
    "anna.schneider@precis-demo.local",
    "max.mueller@precis-demo.local",
    "sofia.wagner@precis-demo.local",
    "jonas.becker@precis-demo.local",
    "lara.fischer@precis-demo.local",
]

WORKLOG_TASKS = {
    "BILLABLE": [
        ("DISCOVERY", "Client discovery and requirements analysis"),
        ("IMPLEMENT", "Implementation delivery"),
        ("ARCHITECTURE", "Solution architecture"),
        ("TESTING", "Client acceptance testing"),
    ],
    "NON_BILLABLE": [
        ("BENCH", "Internal bench / capability development"),
        ("PRESALES", "Presales support"),
        ("TRAINING", "Training and certification"),
    ],
    "INTERNAL": [
        ("ADMIN", "Internal administration"),
        ("MANAGEMENT", "Team management"),
        ("OPS", "Operations support"),
    ],
}


def _pick_supplier(fs_line: str, account_type: str) -> tuple[str | None, str | None]:
    """Return optional supplier metadata for posting-level GL detail."""
    if account_type == "REVENUE" or fs_line in ("BalanceSheet", "Statistical"):
        return None, None
    suppliers = SUPPLIER_BY_FS_LINE.get(fs_line) or SUPPLIER_BY_FS_LINE.get("Other", [])
    if not suppliers:
        return None, None
    # Leave some postings supplier-less, as happens with payroll/accrual journals.
    if random.random() < 0.18:
        return None, None
    return random.choice(suppliers)


def _document_ref(entry_type: str, period: str, line_id: int) -> str | None:
    if random.random() < 0.08:
        return None
    prefix = {
        "REVENUE": "INV",
        "PAYROLL": "PAY",
        "BONUS": "BON",
        "COST": "BILL",
        "SGA": "BILL",
    }.get(entry_type, "JE")
    compact_period = period.replace("-", "")
    return f"{prefix}-{compact_period}-{line_id:06d}"


def build_federated_gl_transactions(entries, lines):
    """Build Postgres-only detail GL transactions for federated read tests."""
    acct_map = {a[0]: a for a in ACCOUNTS}
    je_map = {e["journal_entry_id"]: e for e in entries}
    rows = []

    for line in lines:
        entry = je_map[line["journal_entry_id"]]
        account = acct_map.get(line["account_code"])
        if account is None:
            continue
        account_code, account_name, account_type, fs_line, *_ = account
        supplier_id, supplier_name = _pick_supplier(fs_line, account_type)
        document_ref = _document_ref(entry["entry_type"], entry["period"], line["journal_line_id"])
        approval_status = random.choices(
            ["APPROVED", "PENDING", "REJECTED"],
            weights=[0.92, 0.07, 0.01],
            k=1,
        )[0]
        rows.append({
            "transaction_id": f"GL-{line['journal_line_id']:08d}",
            "journal_entry_id": line["journal_entry_id"],
            "journal_line_id": line["journal_line_id"],
            "scenario": "ACTUALS",
            "entity_id": ENTITY_ID,
            "period": entry["period"],
            "posting_date": entry["entry_date"],
            "document_ref": document_ref,
            "document_type": entry["entry_type"],
            "supplier_id": supplier_id,
            "supplier_name": supplier_name,
            "account_code": account_code,
            "account_name": account_name,
            "account_type": account_type,
            "fs_line": fs_line,
            "cost_centre_id": line["cost_centre_id"],
            "project_id": line["project_id"],
            "debit_amount": line["debit_amount"],
            "credit_amount": line["credit_amount"],
            "amount": line["amount"],
            "currency": line["currency"],
            "description": line["description"] or entry["description"],
            "source_system": random.choice(["ERP", "NETSUITE", "SAP_FI"]),
            "posted_by": random.choice(POSTED_BY_USERS),
            "approval_status": approval_status,
            "created_at": entry["created_at"],
        })

    return rows


def build_federated_worklog_detail(timesheets, employees, projects, clients):
    """Build Postgres-only enriched worklog rows for federated read tests."""
    emp_map = {e["employee_id"]: e for e in employees}
    project_map = {p["project_id"]: p for p in projects}
    client_map = {c["client_id"]: c for c in clients}
    rows = []

    for ts in timesheets:
        emp = emp_map.get(ts["employee_id"])
        project = project_map.get(ts["project_id"])
        if emp is None or project is None:
            continue
        client = client_map.get(project["client_id"], {})
        year, month = [int(x) for x in ts["period"].split("-")]
        day = random.randint(1, 25)
        work_date = datetime.date(year, month, min(day, last_business_day(year, month).day))
        while work_date.weekday() >= 5:
            work_date -= datetime.timedelta(days=1)
        activity = ts["activity_type"]
        task_code, task_desc = random.choice(
            WORKLOG_TASKS.get(activity, WORKLOG_TASKS["INTERNAL"])
        )
        daily_rate = emp.get("daily_bill_rate_eur") or 0
        billable_amount = (float(ts["hours_billable"]) / 8.0) * float(daily_rate)
        submitted_at = datetime.datetime.combine(
            work_date + datetime.timedelta(days=random.randint(0, 3)),
            datetime.time(hour=random.randint(8, 18), minute=random.choice([0, 15, 30, 45])),
        )
        approval_status = random.choices(
            ["APPROVED", "SUBMITTED", "DRAFT"],
            weights=[0.88, 0.10, 0.02],
            k=1,
        )[0]
        approved_at = None
        approved_by = None
        if approval_status == "APPROVED":
            approved_at = submitted_at + datetime.timedelta(days=random.randint(0, 4))
            approved_by = random.choice(POSTED_BY_USERS)

        rows.append({
            "worklog_id": f"WL-{ts['timesheet_id']:08d}",
            "timesheet_id": ts["timesheet_id"],
            "scenario": "ACTUALS",
            "period": ts["period"],
            "work_date": work_date,
            "employee_id": ts["employee_id"],
            "employee_code": emp["employee_code"],
            "employee_name": f"{emp['first_name']} {emp['last_name']}",
            "grade": emp["grade"],
            "project_id": ts["project_id"],
            "project_code": project["project_code"],
            "project_name": project["project_name"],
            "client_id": project["client_id"],
            "client_name": client.get("client_name"),
            "cost_centre_id": ts["cost_centre_id"],
            "activity_type": activity,
            "task_code": task_code,
            "task_description": task_desc,
            "hours_worked": ts["hours_worked"],
            "hours_billable": ts["hours_billable"],
            "daily_bill_rate_eur": daily_rate,
            "billable_amount_eur": round(billable_amount, 2),
            "approval_status": approval_status,
            "submitted_at": submitted_at,
            "approved_at": approved_at,
            "approved_by": approved_by,
            "source_system": random.choice(["HARVEST", "TEMPO", "ERP_TIME"]),
            "created_at": ts["created_at"],
        })

    return rows

# semantic.scenarios rows are declared in instance/scenarios.yml and seeded
# by scenario_runner.apply() (see load_clickhouse). The former in-script
# CH_SCENARIOS list moved there so the example instance is the single source
# of the demo scenario registry.


# ──────────────────────────────────────────────────────────────────────────────
# Database loaders
# ──────────────────────────────────────────────────────────────────────────────

def load_postgres(clients, employees, projects, timesheets, payroll, entries, lines, subledger, intercompany):
    print("Loading PostgreSQL...")
    conn = pg_conn()
    cur = conn.cursor()

    # DDL
    cur.execute(PG_DDL)
    conn.commit()

    # 1. accounts (self-referential: insert non-parents first)
    non_parent = [a for a in ACCOUNTS if a[5] is None]
    has_parent  = [a for a in ACCOUNTS if a[5] is not None]
    for acct_batch in [non_parent, has_parent]:
        cur.executemany(
            """INSERT INTO gl.accounts
               (account_code,account_name,account_type,fs_line,normal_balance,parent_code,is_active,created_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            [(a[0], a[1], a[2], a[3], a[4], a[5], True, CREATED_AT_DEFAULT) for a in acct_batch],
        )
    conn.commit()
    print(f"  Inserted {len(ACCOUNTS)} accounts")

    # 1b. cost_centres
    cur.executemany(
        """INSERT INTO master.cost_centres
           (cost_centre_id, cost_centre_name, department, division, entity_id, is_billable)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        [(cc[0], cc[1], cc[2], cc[3], ENTITY_ID, cc[4]) for cc in COST_CENTRES],
    )
    conn.commit()
    print(f"  Inserted {len(COST_CENTRES)} cost centres")

    # 1c. gl.dim_period — the customer warehouse owns the period dimension
    # (the customer_pg__dim_period binding projects it into live.dim_period),
    # so the mock source must carry it.
    cur.execute("""
        DROP TABLE IF EXISTS gl.dim_period;
        CREATE TABLE gl.dim_period (
            period      VARCHAR(7) PRIMARY KEY,
            quarter     VARCHAR(7) NOT NULL,
            fiscal_year VARCHAR(4) NOT NULL
        );
    """)
    cur.executemany(
        "INSERT INTO gl.dim_period (period, quarter, fiscal_year) VALUES (%s, %s, %s)",
        period_dim_rows(),
    )
    conn.commit()
    print(f"  Inserted {len(period_dim_rows())} gl.dim_period rows")

    # 2. employees (reset sequence)
    cur.execute("SELECT setval('hr.employees_employee_id_seq', 1, false)")
    cur.executemany(
        """INSERT INTO hr.employees
           (employee_id,employee_code,first_name,last_name,email,grade,employee_type,role_title,
            cost_centre_id,annual_cost,daily_bill_rate,fte,start_date,end_date,
            is_active,currency,created_at,updated_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        [(e["employee_id"],e["employee_code"],e["first_name"],e["last_name"],e["email"],
          e["grade"],e["employee_type"],e["role_title"],e["cost_centre_id"],e["annual_cost_eur"],
          e["daily_bill_rate_eur"],e["fte"],e["start_date"],e["end_date"],e["is_active"],
          e["currency"],e["created_at"],e["updated_at"]) for e in employees],
    )
    # Fix sequence
    cur.execute("SELECT setval('hr.employees_employee_id_seq', %s)", (max(e["employee_id"] for e in employees),))
    conn.commit()
    print(f"  Inserted {len(employees)} employees")

    # 2b. clients
    cur.executemany(
        """INSERT INTO projects.clients
           (client_id, client_name, industry, tier, country, created_date)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        [(c["client_id"], c["client_name"], c["industry"], c["tier"],
          c["country"], c["created_at"].date() if hasattr(c["created_at"], 'date') else c["created_at"]) for c in clients],
    )
    conn.commit()
    print(f"  Inserted {len(clients)} clients")

    # 3. projects
    cur.execute("SELECT setval('projects.projects_project_id_seq', 1, false)")
    cur.executemany(
        """INSERT INTO projects.projects
           (project_id,project_code,project_name,client_id,client_name,project_type,status,
            start_date,end_date,budget_hours,budget_revenue,contract_value,
            project_manager_id,cost_centre_id,currency,created_at,updated_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        [(p["project_id"],p["project_code"],p["project_name"],p["client_id"],p["client_name"],
          p["project_type"],p["status"],p["start_date"],p["end_date"],p["budget_hours"],
          p["budget_revenue_eur"],p["contract_value_eur"],p["project_manager_id"],
          p["cost_centre_id"],p["currency"],p["created_at"],p["updated_at"]) for p in projects],
    )
    cur.execute("SELECT setval('projects.projects_project_id_seq', %s)", (max(p["project_id"] for p in projects),))
    conn.commit()
    print(f"  Inserted {len(projects)} projects")

    # 4. timesheets
    cur.executemany(
        """INSERT INTO hr.timesheets
           (employee_id,project_id,cost_centre_id,period_start_date,hours_worked,hours_billable,activity_type,created_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
        [(t["employee_id"],t["project_id"],t["cost_centre_id"],
          datetime.date(int(t["period"][:4]), int(t["period"][5:]), 1),
          t["hours_worked"],t["hours_billable"],t["activity_type"],t["created_at"]) for t in timesheets],
    )
    conn.commit()
    print(f"  Inserted {len(timesheets)} timesheets")

    # 5. payroll
    cur.executemany(
        """INSERT INTO hr.payroll
           (employee_id,cost_centre_id,pay_date,gross_salary,employer_contributions,
            bonus,total_cost,currency,created_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        [(p["employee_id"],p["cost_centre_id"],
          datetime.date(int(p["period"][:4]), int(p["period"][5:]), 1),
          p["gross_salary_eur"],
          p["employer_contributions_eur"],p["bonus_eur"],p["total_cost_eur"],
          p["currency"],p["created_at"]) for p in payroll],
    )
    conn.commit()
    print(f"  Inserted {len(payroll)} payroll rows")

    # 6. journal_entries
    cur.execute("SELECT setval('gl.journal_entries_journal_entry_id_seq', 1, false)")
    cur.executemany(
        """INSERT INTO gl.journal_entries
           (journal_entry_id,entry_date,period,entry_type,description,source,entity_id,created_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
        [(e["journal_entry_id"],e["entry_date"],e["period"],e["entry_type"],
          e["description"],e["source"],e["entity_id"],e["created_at"]) for e in entries],
    )
    cur.execute("SELECT setval('gl.journal_entries_journal_entry_id_seq', %s)", (max(e["journal_entry_id"] for e in entries),))
    conn.commit()
    print(f"  Inserted {len(entries)} journal entries")

    # 7. journal_lines
    cur.execute("SELECT setval('gl.journal_lines_journal_line_id_seq', 1, false)")
    cur.executemany(
        """INSERT INTO gl.journal_lines
           (journal_line_id,journal_entry_id,account_code,cost_centre_id,project_id,
            debit_amount,credit_amount,amount,currency,description)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        [(l["journal_line_id"],l["journal_entry_id"],l["account_code"],l["cost_centre_id"],
          l["project_id"],l["debit_amount"],l["credit_amount"],l["amount"],
          l["currency"],l["description"]) for l in lines],
    )
    cur.execute("SELECT setval('gl.journal_lines_journal_line_id_seq', %s)", (max(l["journal_line_id"] for l in lines),))
    conn.commit()
    print(f"  Inserted {len(lines)} journal lines")

    # 8. revenue subledger
    if subledger:
        cur.executemany(
            """INSERT INTO projects.revenue_subledger
               (project_id, recognition_date, cost_centre_id, client_id, project_type,
                recognition_method, currency, hours_worked, hours_billable,
                revenue_recognised, cost_recognised,
                amount_billed, contract_value,
                percent_complete, cum_revenue_recognised, cum_cost_recognised,
                cum_billed, wip_balance, created_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            [(r["project_id"],
              datetime.date(int(r["period"][:4]), int(r["period"][5:]), 1),
              r["cost_centre_id"], r["client_id"],
              r["project_type"], r["recognition_method"], r["currency"],
              r["hours_worked"], r["hours_billable"],
              r["revenue_recognised_eur"], r["cost_recognised_eur"],
              r["amount_billed_eur"], r["contract_value_eur"],
              r["percent_complete"], r["cum_revenue_recognised_eur"], r["cum_cost_recognised_eur"],
              r["cum_billed_eur"], r["wip_balance_eur"], r["created_at"]) for r in subledger],
        )
        conn.commit()
    print(f"  Inserted {len(subledger)} revenue subledger rows")

    # 9. Postgres-only federated detail sources. These are deliberately not
    # mirrored into ClickHouse; they exist to test Ibis-backed read domains.
    federated_gl = build_federated_gl_transactions(entries, lines)
    if federated_gl:
        cur.executemany(
            """INSERT INTO finance.gl_transactions_detail
               (transaction_id, journal_entry_id, journal_line_id, scenario,
                entity_id, period, posting_date, document_ref, document_type,
                supplier_id, supplier_name, account_code, account_name,
                account_type, fs_line, cost_centre_id, project_id,
                debit_amount, credit_amount, amount, debit_credit, currency, description,
                source_system, posted_by, approval_status, created_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                       %s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            [
                (
                    r["transaction_id"], r["journal_entry_id"], r["journal_line_id"],
                    r["scenario"], r["entity_id"], r["period"], r["posting_date"],
                    r["document_ref"], r["document_type"], r["supplier_id"],
                    r["supplier_name"], r["account_code"], r["account_name"],
                    r["account_type"], r["fs_line"], r["cost_centre_id"],
                    r["project_id"], r["debit_amount"], r["credit_amount"],
                    r["amount"],
                    'D' if (r["debit_amount"] or 0) > 0 else 'C',
                    r["currency"], r["description"], r["source_system"],
                    r["posted_by"], r["approval_status"], r["created_at"],
                )
                for r in federated_gl
            ],
        )
        conn.commit()
    print(f"  Inserted {len(federated_gl)} finance.gl_transactions_detail rows")

    federated_worklogs = build_federated_worklog_detail(timesheets, employees, projects, clients)
    if federated_worklogs:
        cur.executemany(
            """INSERT INTO finance.project_worklog_detail
               (worklog_id, timesheet_id, scenario, period, work_date,
                employee_id, employee_code, employee_name, grade, project_id,
                project_code, project_name, client_id, client_name,
                cost_centre_id, activity_type, task_code, task_description,
                hours_worked, hours_billable, daily_bill_rate_eur,
                billable_amount_eur, approval_status, submitted_at,
                approved_at, approved_by, source_system, created_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                       %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            [
                (
                    r["worklog_id"], r["timesheet_id"], r["scenario"], r["period"],
                    r["work_date"], r["employee_id"], r["employee_code"],
                    r["employee_name"], r["grade"], r["project_id"], r["project_code"],
                    r["project_name"], r["client_id"], r["client_name"],
                    r["cost_centre_id"], r["activity_type"], r["task_code"],
                    r["task_description"], r["hours_worked"], r["hours_billable"],
                    r["daily_bill_rate_eur"], r["billable_amount_eur"],
                    r["approval_status"], r["submitted_at"], r["approved_at"],
                    r["approved_by"], r["source_system"], r["created_at"],
                )
                for r in federated_worklogs
            ],
        )
        conn.commit()
    print(f"  Inserted {len(federated_worklogs)} finance.project_worklog_detail rows")

    # 10. intercompany recharges — standalone memo subledger (finance schema)
    if intercompany:
        cur.executemany(
            """INSERT INTO finance.intercompany_transactions
               (intercompany_id, cost_centre_id, counterparty_cc_id, period, amount, currency, created_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            [(r["intercompany_id"], r["cost_centre_id"], r["counterparty_cc_id"],
              r["period"], r["amount"], r["currency"], r["created_at"]) for r in intercompany],
        )
        conn.commit()
    print(f"  Inserted {len(intercompany)} intercompany recharge rows")

    cur.close()
    conn.close()
    print("PostgreSQL: done.")


PLAN_LOAD_ID = "synthetic_data_bootstrap"


def period_dim_rows() -> list[list[str]]:
    """Period dimension rows, 2023-01 through 2026-12 — written to both the
    mock-source Postgres (`gl.dim_period`, read by the dim_period binding)
    and the ClickHouse config copy."""
    rows = []
    for year in range(2023, 2027):
        for month in range(1, 13):
            rows.append([
                f"{year}-{month:02d}",
                f"{year}-Q{(month - 1) // 3 + 1}",
                str(year),
            ])
    return rows


def land_plan_fact_plan(ch, plan_entries) -> None:
    """Open-tier plan landing: static amounts in `live.fact_plan`.

    The generated entries carry `delta_amount` (the forecast is budget rows
    plus adjustment deltas), so collapse to one static amount per
    (period, account, cost_centre, scenario) — the grain fact_plan declares.
    The table itself comes from the instance live DDL; re-applying it is
    idempotent, and the TRUNCATE makes a regen start clean.
    """
    from collections import defaultdict

    from precis_mcp.ingestion.live_ddl_runner import apply_all as apply_live_ddl

    apply_live_ddl(PROJECT_ROOT / "instance" / "live", ch)
    ch.command("TRUNCATE TABLE live.fact_plan")

    sums: dict[tuple, float] = defaultdict(float)
    for e in plan_entries:
        key = (e["period"], e["account"], e["cost_centre"], e["scenario"])
        sums[key] += e["delta_amount"]

    rows = [
        [period, account, cost_centre, scenario, round(amount, 2), PLAN_LOAD_ID]
        for (period, account, cost_centre, scenario), amount in sums.items()
        if abs(amount) >= 0.005
    ]
    ch.insert(
        "live.fact_plan",
        rows,
        column_names=[
            "period", "account_code", "cost_centre", "scenario",
            "amount", "_load_id",
        ],
    )
    print(f"  Inserted {len(rows)} live.fact_plan rows (static amounts)")


def load_clickhouse(budget_entries, forecast_entries, land_plan):
    print("Loading ClickHouse...")
    ch = ch_client()

    # DDL (execute statement by statement). Comment lines are dropped before
    # the split — a `;` inside a comment would otherwise shear it into a
    # comment-only statement (ClickHouse rejects those as "Empty query") plus
    # a garbage tail.
    ddl_sql = "\n".join(
        line for line in CH_DDL.splitlines()
        if not line.strip().startswith("--")
    )
    for stmt in [s.strip() for s in ddl_sql.split(";") if s.strip()]:
        ch.command(stmt)

    # gl.dim_period — 2023-01 through 2026-12
    period_rows = period_dim_rows()
    ch.insert(
        "gl.dim_period",
        period_rows,
        column_names=["period", "quarter", "fiscal_year"],
    )
    print(f"  Inserted {len(period_rows)} gl.dim_period rows")

    # Plan scenarios (budget + forecast) — where they land is the
    # seam to the Précis platform; see the module docstring.
    all_plan = budget_entries + forecast_entries
    land_plan(ch, all_plan)
    print(
        f"  Landed {len(all_plan)} plan entries "
        f"({len(budget_entries)} budget, {len(forecast_entries)} forecast)"
    )

    # semantic.scenarios — canonical scenario registry. Seeded from the
    # example instance/scenarios.yml via the shared scenario_runner — the
    # same path a real deployment uses. CH_DDL dropped the table above so
    # this regen starts clean; the runner re-creates it (CREATE IF NOT
    # EXISTS) and seeds the declared rows.
    from precis_mcp.ingestion import scenario_runner

    _scenarios_yml = PROJECT_ROOT / "instance" / "scenarios.yml"
    _scenario_report = scenario_runner.apply(_scenarios_yml, ch)
    print(
        f"  Seeded {len(_scenario_report.seeded)} semantic.scenarios rows "
        f"from {_scenarios_yml.name}"
    )

    # gl.actuals / gl.timesheets / gl.payroll / gl.revenue_subledger / dim
    # tables — not direct-loaded here; they're ingested from PG via the
    # corresponding customer_pg__* bindings. The synth script writes the
    # PG sources only; trigger_ingestion() at the end of main() runs each
    # binding end-to-end so `live.*` is populated and the `semantic.*`
    # views read clean.

    print("ClickHouse: done (config dims + plan + scenarios — facts and master dims land via ingestion).")


# ──────────────────────────────────────────────────────────────────────────────
# Validation report
# ──────────────────────────────────────────────────────────────────────────────

def run_validation(employees, timesheets, entries_je, lines_all, budget_entries, forecast_entries, subledger):
    print("\n" + "="*60)
    print("VALIDATION REPORT")
    print("="*60)

    acct_map_local = {a[0]: a for a in ACCOUNTS}

    # 1. Double-entry balance (already checked during generation)
    print("[1] Journal entry balance: already validated during generation.")

    # 2. Total revenue per year
    je_period = {e["journal_entry_id"]: e["period"] for e in entries_je}
    rev_by_year: dict[int, float] = {}
    for jl in lines_all:
        acct = acct_map_local.get(jl["account_code"])
        if not acct or acct[2] not in ("REVENUE",):
            continue
        period = je_period.get(jl["journal_entry_id"], "")
        if not period:
            continue
        year = int(period[:4])
        rev_by_year[year] = rev_by_year.get(year, 0.0) + abs(jl["credit_amount"])

    targets = {2023: 15_000_000, 2024: 17_000_000, 2025: 19_000_000}
    for yr, target in targets.items():
        actual = rev_by_year.get(yr, 0)
        pct = actual / target * 100
        status = "OK" if 90 <= pct <= 110 else "WARN"
        print(f"[2] Revenue {yr}: EUR {actual:,.0f} (target {target:,.0f}) → {pct:.1f}%  [{status}]")

    # 3. EBITDA margin check (2025)
    rev_2025 = rev_by_year.get(2025, 1)
    cost_lines_2025 = sum(
        jl["debit_amount"]
        for jl in lines_all
        if je_period.get(jl["journal_entry_id"], "").startswith("2025-")
        and acct_map_local.get(jl["account_code"], ("","","",""))[2]
           in ("DIRECT_COST", "INDIRECT_COST", "SGA", "OTHER_EXPENSE")
    )
    ebitda = rev_2025 - cost_lines_2025
    margin = ebitda / rev_2025 * 100 if rev_2025 else 0
    status = "OK" if 12 <= margin <= 20 else "WARN"
    print(f"[3] EBITDA margin 2025: {margin:.1f}%  [{status}]")

    # 4. Billable-employee utilisation (excludes ADMIN who are always 0% billable)
    billable_emp_ids = {e["employee_id"] for e in employees if e["grade"] != "ADMIN"}
    bill_hours = sum(t["hours_billable"] for t in timesheets if t["employee_id"] in billable_emp_ids)
    total_hours = sum(t["hours_worked"] for t in timesheets if t["employee_id"] in billable_emp_ids)
    util = bill_hours / total_hours * 100 if total_hours else 0
    status = "OK" if 65 <= util <= 80 else "WARN"
    print(f"[4] Billable-employee utilisation: {util:.1f}%  [{status}]")

    # 5. Active headcount at sample month-ends
    sample_months = [("2023", "06"), ("2024", "06"), ("2025", "06")]
    for yr, mo in sample_months:
        sample_date = datetime.date(int(yr), int(mo), 30)
        active = sum(
            1 for e in employees
            if e["start_date"] <= sample_date
            and (e["end_date"] is None or e["end_date"] > sample_date)
        )
        status = "OK" if 105 <= active <= 125 else "WARN"
        print(f"[5] Active headcount {yr}-{mo}: {active}  [{status}]")

    # 6. Orphan FK check (timesheets → employees)
    emp_ids = {e["employee_id"] for e in employees}
    orphan_ts = sum(1 for t in timesheets if t["employee_id"] not in emp_ids)
    print(f"[6] Orphan timesheet rows: {orphan_ts}  [{'OK' if orphan_ts==0 else 'ERROR'}]")

    # 7. Budget non-zero for revenue accounts
    bud_rev = [b for b in budget_entries if b["account"] in ("4100","4200","4300")]
    status = "OK" if bud_rev else "WARN"
    print(f"[7] Budget revenue entries: {len(bud_rev)}  [{status}]")

    # 8. Forecast differs from budget
    bud_total = sum(b["delta_amount"] for b in budget_entries)
    fc_total  = sum(f["delta_amount"] for f in forecast_entries)
    differs = abs(bud_total - fc_total) > 1.0
    print(f"[8] Forecast differs from budget: {'YES' if differs else 'NO'}  [{'OK' if differs else 'WARN'}]")

    # ── Revenue subledger checks ───────────────────────────────────────────
    # 9. Subledger revenue ≈ GL revenue (per period). Memo subledger MUST
    # reconcile to the GL within rounding tolerance.
    sl_rev_by_period: dict[str, float] = {}
    for r in subledger:
        sl_rev_by_period[r["period"]] = sl_rev_by_period.get(r["period"], 0.0) + r["revenue_recognised_eur"]
    gl_rev_by_period: dict[str, float] = {}
    for jl in lines_all:
        acct = acct_map_local.get(jl["account_code"])
        if not acct or acct[2] != "REVENUE":
            continue
        period = je_period.get(jl["journal_entry_id"], "")
        if not period:
            continue
        gl_rev_by_period[period] = gl_rev_by_period.get(period, 0.0) + abs(jl["credit_amount"])
    max_diff = 0.0
    bad_periods = 0
    for period, sl_v in sl_rev_by_period.items():
        gl_v = gl_rev_by_period.get(period, 0.0)
        diff = abs(sl_v - gl_v)
        if diff > max(1.0, sl_v * 0.001):
            bad_periods += 1
        if diff > max_diff:
            max_diff = diff
    status = "OK" if bad_periods == 0 else "ERROR"
    print(f"[9] Subledger ↔ GL revenue reconciliation: max diff EUR {max_diff:,.2f}, "
          f"mismatched periods: {bad_periods}  [{status}]")

    # 10. Cum billed never exceeds contract value for fixed-fee projects
    ff_overbills = 0
    for r in subledger:
        if r["project_type"] != "FIXED_FEE":
            continue
        if r["contract_value_eur"] > 0 and r["cum_billed_eur"] > r["contract_value_eur"] * 1.001:
            ff_overbills += 1
    status = "OK" if ff_overbills == 0 else "WARN"
    print(f"[10] Fixed-fee billings ≤ contract value: overbill rows = {ff_overbills}  [{status}]")

    # 11. Project margin sanity (overall — should land near company gross margin)
    sl_rev = sum(r["revenue_recognised_eur"] for r in subledger)
    sl_cost = sum(r["cost_recognised_eur"] for r in subledger)
    sl_margin = (sl_rev - sl_cost) / sl_rev * 100 if sl_rev else 0
    status = "OK" if 25 <= sl_margin <= 65 else "WARN"
    print(f"[11] Project-level gross margin (subledger): {sl_margin:.1f}%  [{status}]")

    # 12. WIP sanity — closing WIP should be < ~3 months of revenue
    closing_period = max((r["period"] for r in subledger), default="")
    closing_wip = sum(r["wip_balance_eur"] for r in subledger if r["period"] == closing_period)
    avg_monthly_rev = sl_rev / max(1, len({r["period"] for r in subledger}))
    wip_months = closing_wip / avg_monthly_rev if avg_monthly_rev else 0
    status = "OK" if abs(wip_months) < 4 else "WARN"
    print(f"[12] Closing WIP @ {closing_period}: EUR {closing_wip:,.0f} "
          f"({wip_months:+.1f} months of revenue)  [{status}]")

    print("="*60 + "\n")


# ──────────────────────────────────────────────────────────────────────────────
# STEP 13 — CRM dataset (sales pipeline)
#
# A separate inbound source — CSV files dropped into CRM_LANDINGS_DIR, picked
# up by the file_drop ingestion driver under the `crm_filedrop` source. Two
# datasets: `crm_accounts` (CRM customers in the pipeline) and
# `crm_opportunities` (deals).
#
# Deliberately not joined to the delivery world: CRM accounts do not match
# ERP clients by id — agent reasoning bridges the gap at query time. Service
# lines, however, share names with the practice-area dimension on the cost
# centre side so cross-domain aggregation is plausible.
# ──────────────────────────────────────────────────────────────────────────────

import csv

# CRM CSVs land under `${PRECIS_INGEST_UPLOAD_DIR}/crm/` so the file-drop
# source (`crm_filedrop`, kind=http_upload, backend.prefix='crm/') and
# the synthetic generator share one filesystem root. Fallback path
# matches the old default for dev environments that haven't set the env
# var yet.
CRM_LANDINGS_DIR = Path(
    os.getenv("PRECIS_INGEST_UPLOAD_DIR", "/var/lib/precis/ingest/uploads")
) / "crm"

CRM_SERVICE_LINES = [
    "Cloud & Infrastructure",
    "Software Engineering",
    "Cybersecurity",
    "Digital Transformation",
    "Data & Analytics",
]

CRM_ENGAGEMENT_TYPES = (
    ["Time & Materials"] * 50
    + ["Fixed Price"] * 35
    + ["Retainer"] * 15
)

CRM_SOURCES = (
    ["Inbound"] * 30
    + ["Outbound"] * 30
    + ["Partner"] * 20
    + ["Existing Customer"] * 20
)

CRM_SEGMENTS = ["SMB", "Mid-Market", "Enterprise"]
CRM_SEGMENT_WEIGHTS = [25, 55, 20]
CRM_REGIONS = ["DE", "UK", "FR", "NL", "CH", "AT"]
CRM_REGION_WEIGHTS = [40, 20, 15, 12, 8, 5]

# Stage → probability mapping. Six stages: four open, two closed.
CRM_STAGE_PROB = {
    "Discovery":     0.10,
    "Qualified":     0.30,
    "Proposal":      0.60,
    "Negotiation":   0.80,
    "Closed-Won":    1.00,
    "Closed-Lost":   0.00,
}
CRM_OPEN_STAGES = ["Discovery", "Qualified", "Proposal", "Negotiation"]
CRM_OPEN_STAGE_WEIGHTS = [25, 30, 25, 20]   # weighted toward early funnel

CRM_SALES_REPS = [
    "Anja Bauer", "Felix Hoffmann", "Lena Richter", "Markus Klein",
    "Sophie Wagner", "Tobias Schmidt",
]

# Number of CRM accounts and opportunities.
CRM_NUM_ACCOUNTS = 50
CRM_NUM_OPPORTUNITIES = 300

# Opportunity status mix: ~60% open, ~25% won, ~15% lost.
CRM_STATUS_MIX = ["Open"] * 60 + ["Closed-Won"] * 25 + ["Closed-Lost"] * 15

# CRM time horizon: opportunities created between 2023-06-01 and 2026-05-31.
# Open opps close in the future (2026-06 .. 2027-06); closed opps closed in
# the past (anywhere from creation to today).
CRM_CREATED_FROM = datetime.date(2023, 6, 1)
CRM_CREATED_TO   = datetime.date(2026, 5, 31)
CRM_OPEN_CLOSE_FROM = datetime.date(2026, 6, 1)
CRM_OPEN_CLOSE_TO   = datetime.date(2027, 6, 30)


def _crm_segment_amount_band(segment: str) -> tuple[float, float]:
    """Lognormal mu/sigma for opportunity TCV by account segment.

    Calibrated to land Enterprise medians around €350k, Mid-Market around
    €120k, SMB around €40k, with long right tails.
    """
    if segment == "Enterprise":
        return (12.8, 0.7)   # median ~e^12.8 ≈ 360k
    if segment == "Mid-Market":
        return (11.7, 0.6)   # median ~120k
    return (10.6, 0.55)      # SMB median ~40k


def generate_crm_accounts():
    """~50 CRM accounts. Deliberately distinct from delivery `clients`."""
    accounts = []
    for i in range(1, CRM_NUM_ACCOUNTS + 1):
        segment = random.choices(CRM_SEGMENTS, weights=CRM_SEGMENT_WEIGHTS)[0]
        region = random.choices(CRM_REGIONS, weights=CRM_REGION_WEIGHTS)[0]
        # Created any time in the last ~5 years.
        created = fake.date_between(
            start_date=datetime.date(2021, 1, 1),
            end_date=datetime.date(2026, 4, 30),
        )
        accounts.append({
            "account_id": f"ACC-{i:04d}",
            "account_name": fake.company(),
            "industry": random.choice(CLIENT_INDUSTRIES),
            "region": region,
            "segment": segment,
            "created_date": created.isoformat(),
        })
    return accounts


def _generate_one_opportunity(opp_idx: int, accounts: list[dict]) -> dict:
    account = random.choice(accounts)
    status = CRM_STATUS_MIX[opp_idx % len(CRM_STATUS_MIX)]

    if status == "Open":
        stage = random.choices(CRM_OPEN_STAGES, weights=CRM_OPEN_STAGE_WEIGHTS)[0]
        stage_category = "Open"
        close_date = fake.date_between(
            start_date=CRM_OPEN_CLOSE_FROM, end_date=CRM_OPEN_CLOSE_TO,
        )
    elif status == "Closed-Won":
        stage = "Closed-Won"
        stage_category = "Won"
        close_date = fake.date_between(
            start_date=CRM_CREATED_FROM, end_date=CRM_CREATED_TO,
        )
    else:
        stage = "Closed-Lost"
        stage_category = "Lost"
        close_date = fake.date_between(
            start_date=CRM_CREATED_FROM, end_date=CRM_CREATED_TO,
        )

    probability = CRM_STAGE_PROB[stage]

    # Created somewhere between 30d and 18mo before close_date, but not
    # before the global creation horizon.
    earliest_created = max(
        CRM_CREATED_FROM,
        close_date - datetime.timedelta(days=540),
    )
    latest_created = min(
        close_date - datetime.timedelta(days=30),
        CRM_CREATED_TO,
    )
    if earliest_created >= latest_created:
        created_date = earliest_created
    else:
        created_date = fake.date_between(
            start_date=earliest_created, end_date=latest_created,
        )

    # Last stage change: for closed opps, == close_date; for open, somewhere
    # between created and today. If created_date is already past today (open
    # opp with future close, recent creation), use created_date as-is.
    if stage_category in ("Won", "Lost"):
        last_stage_change = close_date
    else:
        today_bound = datetime.date(2026, 5, 14)
        if created_date >= today_bound:
            last_stage_change = created_date
        else:
            last_stage_change = fake.date_between(
                start_date=created_date, end_date=today_bound,
            )

    # TCV — lognormal by segment.
    mu, sigma = _crm_segment_amount_band(account["segment"])
    amount = float(np.random.lognormal(mu, sigma))
    # Round to nearest €500 for plausibility.
    amount = round(amount / 500.0) * 500.0

    duration_months = int(np.clip(
        round(np.random.lognormal(2.1, 0.5)), 3, 24
    ))

    service_line = random.choice(CRM_SERVICE_LINES)
    engagement_type = random.choice(CRM_ENGAGEMENT_TYPES)
    owner = random.choice(CRM_SALES_REPS)
    source = random.choice(CRM_SOURCES)

    # Estimated start: 2-6 weeks after close (when delivery would begin).
    est_start = close_date + datetime.timedelta(days=random.randint(14, 42))

    # Opportunity name: "<account> — <service line> <year>"
    opp_name = f"{account['account_name']} — {service_line} {close_date.year}"

    return {
        "opportunity_id": f"OPP-{opp_idx:05d}",
        "account_id": account["account_id"],
        "opportunity_name": opp_name,
        "stage": stage,
        "stage_category": stage_category,
        "probability": f"{probability:.4f}",
        "amount": f"{amount:.2f}",
        "currency": "EUR",
        "created_date": created_date.isoformat(),
        "close_date": close_date.isoformat(),
        "last_stage_change_date": last_stage_change.isoformat(),
        "owner": owner,
        "service_line": service_line,
        "engagement_type": engagement_type,
        "duration_months": duration_months,
        "estimated_start_date": est_start.isoformat(),
        "source": source,
    }


def generate_crm_opportunities(accounts: list[dict]):
    """~300 opportunities distributed Open/Won/Lost ≈ 60/25/15."""
    # Shuffle the status mix so opp ids aren't sequential by status.
    indexed = list(range(CRM_NUM_OPPORTUNITIES))
    random.shuffle(indexed)
    opportunities = []
    for i in range(1, CRM_NUM_OPPORTUNITIES + 1):
        opportunities.append(_generate_one_opportunity(i, accounts))
    return opportunities


def write_crm_csvs(accounts: list[dict], opportunities: list[dict]) -> None:
    """Write CRM CSVs to CRM_LANDINGS_DIR, replacing any prior files."""
    CRM_LANDINGS_DIR.mkdir(parents=True, exist_ok=True)

    accounts_path = CRM_LANDINGS_DIR / "accounts.csv"
    with accounts_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(accounts[0].keys()))
        writer.writeheader()
        writer.writerows(accounts)

    opps_path = CRM_LANDINGS_DIR / "opportunities.csv"
    with opps_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(opportunities[0].keys()))
        writer.writeheader()
        writer.writerows(opportunities)

    print(f"  → wrote {accounts_path} ({len(accounts)} rows)")
    print(f"  → wrote {opps_path} ({len(opportunities)} rows)")


# ──────────────────────────────────────────────────────────────────────────────
# STEP 14 — Ingestion trigger
#
# After PG and (config-only) CH writes complete, drive the ingestion subsystem
# through each registered binding end-to-end. Uses the in-process orchestrator
# (`run_binding`) rather than the HTTP push endpoint to avoid the API-server +
# auth setup. Production wiring runs the same `run_binding` behind the push
# route; the smoke test exercises orchestrator + drivers + landing + dbt
# refresh, not the route's auth gate.
#
# Pre-conditions (the script does NOT enforce these — operator responsibility):
#   - Landing tables exist in CH. Run
#     `python scripts/migrations/clickhouse/landing_ddl_generator.py --apply`
#     once after registry changes.
#   - ClickHouse named collections registered for the `customer_pg` source
#     (the API server does this at startup; if running this script standalone
#     against a fresh CH, register them via
#     `precis_mcp.ingestion.named_collections.register_named_collections`).
# ──────────────────────────────────────────────────────────────────────────────


INGESTION_TRIGGERED_BY = "synthetic_data_bootstrap"


def synth_period_provider(binding) -> list[Optional[str]]:
    """Period list per binding for the synth bootstrap.

    Deliberately bypasses `period_selection` (the BAU strategy used by the
    cron / push triggers): a fresh dev box has no watermark and binding
    `lookback_periods` are tuned for steady-state, not for a 36-month
    backfill. The synth generator wrote rows across `ACTUALS_START`..
    `ACTUALS_END`; this provider lands every one of them.

    Snapshot bindings → `[None]` (one shot, no period axis).
    Period bindings → the full generated actuals span.
    """
    if binding.kind == "snapshot":
        return [None]
    return [period_str(y, m) for y, m in months_in_range(ACTUALS_START, ACTUALS_END)]


def drive_bindings(registry, ctx, period_provider_fn, run_binding_fn) -> tuple[int, int]:
    """Pure orchestration loop — iterate every binding, ask
    `period_provider_fn` for the periods to load, invoke `run_binding_fn`
    once per (binding, period). Returns (total_ok, total_failed).

    Decoupled from `period_selection` so the synth bootstrap can land the
    full generated history; pass `select_periods`-backed provider for a BAU
    smoke test instead.
    """
    total_ok = 0
    total_failed = 0
    for binding_id, binding in sorted(registry.bindings.items()):
        periods = period_provider_fn(binding)
        ok = 0
        failed = 0
        for period in periods:
            try:
                result = run_binding_fn(
                    ctx, binding_id, period, triggered_by=INGESTION_TRIGGERED_BY
                )
                if getattr(result, "status", "success") == "success":
                    ok += 1
                else:
                    failed += 1
            except Exception as exc:
                failed += 1
                print(f"  {binding_id} {period}: ERROR {exc}")
        total_ok += ok
        total_failed += failed
        print(f"  {binding_id}: {ok}/{len(periods)} periods loaded ({failed} failed)")
    print(
        f"Ingestion: {total_ok} loads OK, {total_failed} failed across "
        f"{len(registry.bindings)} bindings."
    )
    return total_ok, total_failed


def trigger_ingestion() -> None:
    """Bootstrap CH, drive every binding for every period its
    `period_selection` declares, then refresh the semantic views.

    The full Ibis-driven pipeline for a fresh dev environment:

      1. Apply `instance/live/*.sql` — create `live.<x>` + `staging.<x>`
         tables in CH (idempotent; existing tables left alone).
      2. For each binding × period, run `extract → validate → swap`.
         PG-backed bindings pull aggregations source-side via Ibis;
         file-drop bindings read CSVs via DuckDB. Each load lands rows
         in `staging.<x>`, validates the shape, and atomically swaps
         into `live.<x>`.
      3. Apply `instance/semantic/{dims,views}/*.sql` — every view is
         a fresh `CREATE OR REPLACE VIEW semantic.<name>` pointing at
         the just-populated `live.*` tables.

    The single `build_default_context` call wires the
    OrchestratorContext from env vars — no driver registry, no dbt.
    """
    from precis_mcp.db import get_clickhouse_client
    from precis_mcp.engine import load_and_validate
    from precis_mcp.ingestion.live_ddl_runner import apply_all as apply_live_ddl
    from precis_mcp.ingestion.orchestrator import run_binding
    from precis_mcp.ingestion.registry import IntegrationRegistry
    from precis_mcp.ingestion.semantic_runner import apply_all as apply_semantic
    from precis_mcp.ingestion.wiring import build_default_context

    print("\nStep 14: Triggering ingestion bindings...")

    instance_root = PROJECT_ROOT / "instance"
    registry = IntegrationRegistry.load(instance_root / "integrations")

    # File-drop bindings (kind=http_upload) resolve `${source_path}`
    # from `PRECIS_INGEST_UPLOAD_DIR` + `source.backend.prefix`. The
    # synthetic generator writes CRM CSVs under the same root
    # (CRM_LANDINGS_DIR derives from PRECIS_INGEST_UPLOAD_DIR), so the
    # two sides agree on the filesystem layout.
    os.environ.setdefault(
        "PRECIS_INGEST_UPLOAD_DIR", str(CRM_LANDINGS_DIR.parent)
    )

    # 1. Bootstrap CH tables — required before any binding can swap into
    #    live. CREATE IF NOT EXISTS, so safe to re-run.
    ch = get_clickhouse_client()
    print("  Applying instance/live/*.sql (live + staging tables)...")
    apply_live_ddl(instance_root / "live", ch)

    # 2. Drive every binding × period through the orchestrator. The
    #    synth provider ignores binding.period_selection on purpose — we
    #    want a full-history backfill of the rows the generator just
    #    wrote, not the steady-state lookback window.
    ctx = build_default_context(registry)
    drive_bindings(
        registry, ctx, synth_period_provider, run_binding_fn=run_binding,
    )

    # 3. Refresh semantic views so downstream readers (the engine, the
    #    agent, the UI) see the fresh data through `semantic.v_*` /
    #    `semantic.dim_*` rather than a stale prior definition. The catalogue
    #    is passed so `apply_all` also materialises the auto pass-through
    #    `semantic.dim_*` and ragged-hierarchy views, matching what
    #    `clickhouse_init` provisions.
    print("  Applying instance/semantic/*.sql (views)...")
    catalogue = load_and_validate(str(instance_root / "catalogue"))
    apply_semantic(instance_root / "semantic", ch, catalogue=catalogue)


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main(land_plan=land_plan_fact_plan):
    print("=== FP&A Synthetic Data Generator ===")
    print(f"Seed: {SEED}  |  Actuals: 2023-2025  |  Budget/Forecast: 2026\n")

    print("Step 0: Bootstrapping Postgres (mock-source DB + platform schema)...")
    ensure_environment()

    print("Step 1: Generating employees...")
    employees, cost_history = generate_employees()
    print(f"  {len(employees)} employees, {len(cost_history)} cost history records")

    print("Step 2: Generating clients...")
    clients = generate_clients()
    print(f"  {len(clients)} clients")

    print("Step 3: Generating projects + assignments...")
    projects, assignments = generate_projects(employees, clients)
    assignments = fill_assignment_gaps(employees, projects, assignments)
    print(f"  {len(projects)} projects, {len(assignments)} assignments (after gap-fill)")

    print("Step 4: Generating timesheets (36 months)...")
    timesheets = generate_timesheets(employees, assignments)
    print(f"  {len(timesheets)} timesheet rows")

    print("Step 5: Generating payroll (36 months)...")
    payroll = generate_payroll(employees, cost_history)
    print(f"  {len(payroll)} payroll rows")

    print("Step 6: Building revenue subledger (project × period)...")
    subledger = compute_revenue_subledger(projects, timesheets, employees, cost_history)
    print(f"  {len(subledger)} subledger rows")

    print("Step 7: Generating journal entries...")
    entries_je, lines_all = generate_journal_entries(projects, employees, timesheets, payroll, subledger)
    print(f"  {len(entries_je)} journal entries, {len(lines_all)} journal lines")

    print("Step 8: Validating double-entry balance...")
    validate_journal_entries(entries_je, lines_all)

    print("Step 9: Generating budget (BUD-2026)...")
    budget_entries = generate_budget_entries(lines_all, entries_je, employees)
    print(f"  {len(budget_entries)} budget rows")

    print("Step 10: Generating forecast (FC-2026-Q1)...")
    forecast_entries = generate_forecast_entries(budget_entries)
    print(f"  {len(forecast_entries)} forecast rows")

    print("Step 13: Generating CRM accounts + opportunities...")
    crm_accounts = generate_crm_accounts()
    crm_opportunities = generate_crm_opportunities(crm_accounts)
    print(f"  {len(crm_accounts)} accounts, {len(crm_opportunities)} opportunities")
    write_crm_csvs(crm_accounts, crm_opportunities)

    print("Step 12: Generating intercompany recharges (36 months)...")
    intercompany = generate_intercompany()
    print(f"  {len(intercompany)} intercompany recharge rows")

    print("\nLoading data into PostgreSQL...")
    load_postgres(clients, employees, projects, timesheets, payroll, entries_je, lines_all, subledger, intercompany)

    print("\nLoading data into ClickHouse (config dims + plan only)...")
    load_clickhouse(budget_entries, forecast_entries, land_plan)

    trigger_ingestion()

    run_validation(employees, timesheets, entries_je, lines_all, budget_entries, forecast_entries, subledger)
    print("Done.")


if __name__ == "__main__":
    main()
