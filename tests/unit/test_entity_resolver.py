"""Entity resolution tests, built from the real production transcript.

The customer names here are copied verbatim from live data, including the
double-pasted "AL QASSEM..." and the stray space in "S.A.R.L .". Exact
matching is unusable against input like this.
"""

import sqlite3

import pytest

from app.discovery.entity_resolver import (
    build_entity_index,
    core_form,
    looks_like_code,
    normalise,
)
from app.discovery.models import ObjectMeta
from app.execution.executor import ReadOnlyExecutor

LIVE_CUSTOMERS = [
    ("C00075", "CASH CUSTOMER"),
    ("C00472", "D.A.E.Y. ERYON LTD"),
    ("C00594", "M. M MANSUR GROUP"),
    ("C00541", "MRE AUTO HOLDINGS PTY LTD T/A RENNEN AUTOTEILE"),
    ("C00054", "ARGENTO TRADING 56 CC PTY LTD T/A MBC PARTS"),
    ("C00187", "ZAATRE EXPRESS LTD"),
    ("C00186", "ZAATRE EXPRESS LTD - WAFEEQ"),
    ("C06853", "MERSIN TRADE"),
    ("C00124", "KARAOUI PIECE MTO S.A.R.L ."),
    ("C00104", "HALA CAR CO"),
    ("C00356", "AL QASSEM USED CARS TR. LLCAL QASSEM USED CARS TR. LLC"),
    ("C00011", "YASSIR AWAWDEH AUTO SPARE PARTS TR LLC SOLE PROPRIETORSHIP"),
    ("C00010", "AWAWDEH AUTO.SPARE PARTS L.L.C S.P"),
]


@pytest.fixture()
def index(tmp_path):
    path = tmp_path / "entities.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE dim_b3_customer(customer_code TEXT, customer_name TEXT)")
    conn.executemany("INSERT INTO dim_b3_customer VALUES (?,?)", LIVE_CUSTOMERS)
    conn.execute("CREATE TABLE dim_b3_item(item_code TEXT, item_name TEXT)")
    conn.executemany(
        "INSERT INTO dim_b3_item VALUES (?,?)",
        [
            ("1ZS 011 939-411", "SKODA SUPERB (2016-2019). HEADLAMP XENON LED - LH. 3V1941017A"),
            ("1ZS 011 939-421", "SKODA SUPERB (2016-2019). HEADLAMP XENON LED - RH. 3V1941018A"),
        ],
    )
    conn.commit()
    conn.close()

    executor = ReadOnlyExecutor(path, query_timeout_s=10, max_rows=5000)
    objects = {
        "dim_b3_customer": ObjectMeta(
            name="dim_b3_customer", kind="table", category="dim",
            columns=["customer_code", "customer_name"],
        ),
        "dim_b3_item": ObjectMeta(
            name="dim_b3_item", kind="table", category="dim",
            columns=["item_code", "item_name"],
        ),
    }
    yield build_entity_index(executor, objects)
    executor.close()


# --------------------------------------------------------------- the real bug


def test_the_mansur_case(index):
    """THE production failure.

    User typed "M. M MANSUR GROUP"; the routing model normalised it to
    "M. M. Mansur Group"; SQLite's byte-exact `=` returned zero rows; the
    system told a user that its own third-largest customer did not exist.
    """
    outcome = index.resolve("customer_name", "M. M. Mansur Group")
    assert outcome.status == "resolved"
    assert outcome.value == "M. M MANSUR GROUP"
    assert outcome.usable


@pytest.mark.parametrize(
    "typed",
    [
        "M. M MANSUR GROUP",      # exactly as stored
        "m. m mansur group",      # lower case
        "M M MANSUR GROUP",       # no punctuation
        "M.M. Mansur Group",      # different spacing
        "  M. M. Mansur Group  ", # padded
    ],
)
def test_mansur_survives_every_plausible_spelling(index, typed):
    outcome = index.resolve("customer_name", typed)
    assert outcome.usable
    assert outcome.value == "M. M MANSUR GROUP"


# ------------------------------------------------------------------ behaviour


def test_exact_stored_value_passes_through_untouched(index):
    outcome = index.resolve("customer_name", "CASH CUSTOMER")
    assert outcome.status == "exact"
    assert outcome.value == "CASH CUSTOMER"


def test_code_given_where_a_name_is_expected(index):
    """"can we expand more on customer C00075" — the filter column is
    customer_name but the user supplied a code."""
    outcome = index.resolve("customer_name", "C00075")
    assert outcome.status == "resolved"
    assert outcome.value == "CASH CUSTOMER"


def test_legal_suffix_noise_is_ignored(index):
    outcome = index.resolve("customer_name", "Mersin Trade LLC")
    assert outcome.usable
    assert outcome.value == "MERSIN TRADE"


def test_trailing_punctuation_mess(index):
    outcome = index.resolve("customer_name", "Karaoui Piece MTO SARL")
    assert outcome.usable
    assert outcome.value == "KARAOUI PIECE MTO S.A.R.L ."


def test_ambiguity_asks_instead_of_guessing(index):
    """Two Zaatre entities exist. Silently picking one would be worse than
    an empty result — it would be a confidently wrong number."""
    outcome = index.resolve("customer_name", "Zaatre Express")
    assert outcome.status in {"ambiguous", "resolved"}
    if outcome.status == "ambiguous":
        assert len(outcome.candidates) >= 2
        assert not outcome.usable


def test_genuinely_absent_name_is_reported_as_unknown(index):
    outcome = index.resolve("customer_name", "Wayne Enterprises")
    assert outcome.status == "unknown"
    assert not outcome.usable


def test_unindexed_column_never_blocks_the_query(index):
    """A column we have no dimension for must pass through unchanged rather
    than fail closed."""
    outcome = index.resolve("warehouse_name", "Main")
    assert outcome.status == "no_index"
    assert outcome.value == "Main"
    assert outcome.usable


def test_item_codes_resolve_too(index):
    outcome = index.resolve("item_code", "1ZS 011 939-411")
    assert outcome.status == "exact"


# --------------------------------------------------------------- normalisation


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("M. M MANSUR GROUP", "m m mansur group"),
        ("KARAOUI PIECE MTO S.A.R.L .", "karaoui piece mto s a r l"),
        ("AWAWDEH AUTO.SPARE PARTS L.L.C S.P", "awawdeh auto spare parts l l c s p"),
    ],
)
def test_normalise(raw, expected):
    assert normalise(raw) == expected


def test_core_form_strips_legal_forms():
    assert core_form("MERSIN TRADE LLC") == core_form("Mersin Trade")


@pytest.mark.parametrize("value", ["C00075", "C06853", "1ZS 011 939-411"])
def test_code_detection(value):
    assert looks_like_code(value)


@pytest.mark.parametrize("value", ["CASH CUSTOMER", "Mersin Trade"])
def test_names_are_not_codes(value):
    assert not looks_like_code(value)
