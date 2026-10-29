"""SOP rules: exact boundaries, properties over all inputs, and traceability to the SOP text."""

import ast
import re
from datetime import datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from engine.cargo import load_cargo_types
from engine.rules.engine import RULE_IDS, RuleInput, RulesEngine, load_rule_book
from engine.settings import PROJECT_ROOT, SOP_PATH
from engine.sop.retriever import parse_sop_clauses

ENGINE = RulesEngine()
CARGO = load_cargo_types()


def reading(**kw: object) -> RuleInput:
    fields: dict[str, object] = {
        "shipment_id": "S1",
        "seq": 1,
        "reading_ts": datetime(2026, 3, 2, 12, 0),
        "depot_id": "DEP-LB",
        "cargo_type": "FRESH",
        "temperature_c": 2.0,
        "port_congestion": None,
        "risk_classification": "Low Risk",
        "delay_probability": 0.1,
        "eta_delay_min": 0.0,
    }
    fields.update(kw)
    return RuleInput(**fields)  # type: ignore[arg-type]


def rule_ids(r: RuleInput, dq: str = "valid") -> set[str]:
    return {h.rule_id for h in ENGINE.evaluate(r, dq)}  # type: ignore[arg-type]


# --- boundaries from the build prompt: 4.0 vs 4.01, 7.0 vs 7.01, 0.65 vs 0.66 ----------------


@pytest.mark.parametrize(("temp", "hit"), [(4.0, False), (4.01, True), (3.99, False)])
def test_r_temp_01_fresh_boundary(temp: float, hit: bool) -> None:
    assert ("R-TEMP-01" in rule_ids(reading(temperature_c=temp))) is hit


@pytest.mark.parametrize(("level", "hit"), [(7.0, False), (7.01, True), (6.99, False)])
def test_r_cong_01_boundary(level: float, hit: bool) -> None:
    assert ("R-CONG-01" in rule_ids(reading(port_congestion=level))) is hit


@pytest.mark.parametrize(("prob", "hit"), [(0.65, False), (0.66, True), (0.651, True)])
def test_r_risk_01_boundary(prob: float, hit: bool) -> None:
    r = reading(risk_classification="High Risk", delay_probability=prob)
    assert ("R-RISK-01" in rule_ids(r)) is hit


@pytest.mark.parametrize(("delay", "hit"), [(60.0, False), (60.1, True)])
def test_r_eta_01_boundary(delay: float, hit: bool) -> None:
    r = reading(temperature_c=5.0, eta_delay_min=delay)
    assert ("R-ETA-01" in rule_ids(r)) is hit


def test_r_risk_01_needs_high_risk_class() -> None:
    assert rule_ids(reading(risk_classification="Moderate Risk", delay_probability=0.99)) == set()


def test_r_eta_01_needs_a_temperature_breach() -> None:
    assert rule_ids(reading(temperature_c=3.0, eta_delay_min=180.0)) == set()


def test_port_congestion_missing_is_not_a_hit() -> None:
    assert rule_ids(reading(port_congestion=None)) == set()


@pytest.mark.parametrize("code", ["FROZEN", "PHARMA"])
def test_other_cargo_bands_use_their_configured_max(code: str) -> None:
    band = CARGO[code]
    assert "R-TEMP-01" not in rule_ids(reading(cargo_type=code, temperature_c=band.max_c))
    assert "R-TEMP-01" in rule_ids(reading(cargo_type=code, temperature_c=band.max_c + 0.01))


def test_unknown_cargo_type_never_breaches() -> None:
    assert rule_ids(reading(cargo_type="UNKNOWN", temperature_c=40.0)) == set()


# --- properties over all inputs ---------------------------------------------------------------

temps = st.floats(min_value=-40, max_value=60, allow_nan=False)
probs = st.floats(min_value=0, max_value=1, allow_nan=False)


@given(temp=temps, code=st.sampled_from(sorted(CARGO)))
def test_temperature_rule_is_exactly_strict_greater_than_band_max(temp: float, code: str) -> None:
    hit = "R-TEMP-01" in rule_ids(reading(cargo_type=code, temperature_c=temp))
    assert hit == (temp > CARGO[code].max_c)


@given(level=st.floats(min_value=0, max_value=10, allow_nan=False))
def test_congestion_rule_is_exactly_strict_greater_than_seven(level: float) -> None:
    assert ("R-CONG-01" in rule_ids(reading(port_congestion=level))) == (level > 7.0)


@given(prob=probs, cls=st.sampled_from(["Low Risk", "Moderate Risk", "High Risk"]))
def test_risk_rule_matches_the_sop_condition(prob: float, cls: str) -> None:
    hit = "R-RISK-01" in rule_ids(reading(risk_classification=cls, delay_probability=prob))
    assert hit == (cls == "High Risk" and prob > 0.65)


@given(temp=temps, delay=st.floats(min_value=0, max_value=300, allow_nan=False))
def test_eta_rule_implies_temperature_rule(temp: float, delay: float) -> None:
    ids = rule_ids(reading(temperature_c=temp, eta_delay_min=delay))
    assert ("R-ETA-01" in ids) == ("R-TEMP-01" in ids and delay > 60.0)


@given(temp=temps, prob=probs)
def test_rules_are_deterministic(temp: float, prob: float) -> None:
    r = reading(temperature_c=temp, risk_classification="High Risk", delay_probability=prob)
    assert ENGINE.evaluate(r, "valid") == ENGINE.evaluate(r, "valid")


# --- data quality ----------------------------------------------------------------------------


def test_invalid_readings_are_never_evaluated() -> None:
    assert rule_ids(reading(temperature_c=30.0), dq="invalid") == set()


def test_suspect_hits_carry_their_status() -> None:
    hits = ENGINE.evaluate(reading(temperature_c=30.0), "suspect")
    assert [h.dq_status for h in hits] == ["suspect"]


def test_hit_records_evidence_and_sop_link() -> None:
    (hit,) = ENGINE.evaluate(reading(temperature_c=6.5), "valid")
    assert hit.observed == {"temperature_c": 6.5, "cargo_type": "FRESH"}
    assert hit.threshold == {"max_c": 4.0}
    assert hit.sop_ref == "cold_chain_sop.md §1"


# --- traceability: the YAML says what the SOP says --------------------------------------------


def test_every_rule_quotes_its_sop_clause_verbatim() -> None:
    book = load_rule_book()
    clauses = {c.clause_id: c.text for c in parse_sop_clauses(SOP_PATH.read_text("utf-8"), "")}
    assert tuple(book.rules) == RULE_IDS
    for rule in book.rules.values():
        assert rule.sop_quote in clauses[rule.sop_clause], rule.rule_id


def test_thresholds_in_the_yaml_match_the_sop() -> None:
    book = load_rule_book()
    assert CARGO["FRESH"].max_c == book.rule("R-TEMP-01").sop_threshold == 4.0
    assert book.rule("R-CONG-01").port_congestion_gt == book.rule("R-CONG-01").sop_threshold
    assert book.rule("R-RISK-01").delay_probability_gt == book.rule("R-RISK-01").sop_threshold
    assert book.rule("R-ETA-01").eta_delay_min_gt == 60.0  # SOP: "more than 60 minutes"


# --- no LLM anywhere in the deterministic core ----------------------------------------------

DETERMINISTIC = [
    "engine/rules",
    "engine/risk",
    "engine/incidents",
    "engine/dq",
    "engine/detection.py",
]
LLM_MODULES = re.compile(r"^(langchain|langgraph|openai|ollama|anthropic|langchain_\w+)(\.|$)")


@pytest.mark.parametrize("target", DETERMINISTIC)
def test_deterministic_core_imports_no_llm_library(target: str) -> None:
    path = PROJECT_ROOT / target
    files = [path] if path.is_file() else sorted(path.rglob("*.py"))
    assert files
    for file in files:
        for node in ast.walk(ast.parse(file.read_text("utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert not LLM_MODULES.match(name), f"{file.name} imports {name}"
