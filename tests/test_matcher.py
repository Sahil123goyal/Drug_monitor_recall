"""Synthetic cases covering deterministic matches and false-positive boundaries."""

from datetime import date, datetime, timezone

import pytest

from app.models import Medicine as MedicineRecord
from app.models import Notice as NoticeRecord
from app.models.enums import Verdict
from app.schemas.medicine import Medicine
from app.schemas.notice import Notice
from app.services.matcher import match, normalize_name, normalize_strength


@pytest.fixture
def medicine() -> Medicine:
    return Medicine(name="DemoRelief", strength="500 mg", batch_no="SYN001")


@pytest.fixture
def notice() -> Notice:
    return Notice(
        recall_number="SYN-RECALL-001",
        product_description="DemoRelief 500mg tablets, 100 tablets per bottle",
        code_info="Lot numbers: SYN001, SYN002",
        url="https://api.fda.gov/drug/enforcement.json?search=recall_number:SYN-RECALL-001",
        report_date=date(2026, 1, 15),
        fetched_at=datetime(2026, 1, 16, tzinfo=timezone.utc),
        raw_json={"synthetic": True},
    )


def test_match_includes_reproducible_evidence(medicine: Medicine, notice: Notice) -> None:
    decision = match(medicine, notice)
    assert decision.verdict == Verdict.MATCH
    assert decision.evidence["brand"]["score"] == 100
    assert decision.evidence["strength"]["normalized"] == "500mg"
    assert decision.evidence["batch"]["tokens"] == ["SYN001", "SYN002"]
    assert decision.evidence["batch"]["matched"] is True
    assert decision.evidence["notice"]["url"] == str(notice.url)
    assert decision.evidence["notice"]["report_date"] == "2026-01-15"
    assert decision.evidence["requires_user_input"] is False
    assert match(medicine, notice) == decision
    assert '"MATCH"' in decision.model_dump_json()


@pytest.mark.parametrize("name", ["DEMORELIEF", "  DemoRelief!  ", "DemoRelief®"])
def test_name_case_and_punctuation(medicine: Medicine, notice: Notice, name: str) -> None:
    assert match(medicine.model_copy(update={"name": name}), notice).verdict == Verdict.MATCH


@pytest.mark.parametrize("name", ["DemoRelif", "DemoReleif", "DemoRellief"])
def test_spelling_variants_use_rapidfuzz_threshold(
    medicine: Medicine, notice: Notice, name: str,
) -> None:
    modified = notice.model_copy(update={"product_description": f"{name} 500 mg tablets"})
    decision = match(medicine, modified)
    assert decision.verdict == Verdict.MATCH
    assert 90 <= decision.evidence["brand"]["score"] < 100
    assert match(medicine, modified, brand_threshold=100).verdict == Verdict.NO_MATCH


@pytest.mark.parametrize("value, expected", [
    ("500 mg", "500mg"), ("500MG", "500mg"), ("500.00 mg", "500mg"),
    ("0.5 mg", "0.5mg"), (".5mg", "0.5mg"), ("500 mcg", "500mcg"),
    ("500 µg", "500mcg"), ("500 μg", "500mcg"), ("10 %", "10%"),
    ("250 mg / 5 mL", "250mg/5ml"), ("250mg per 5ml", "250mg/5ml"),
    ("10mg/mL", "10mg/1ml"), ("500", None), ("500mg2", None), ("unknown", None),
])
def test_strength_normalization(value: str, expected: str | None) -> None:
    assert normalize_strength(value) == expected


@pytest.mark.parametrize("strength", ["500mg", "500 MG", "500.0 mg"])
def test_equivalent_strengths_match(medicine: Medicine, notice: Notice, strength: str) -> None:
    assert match(medicine.model_copy(update={"strength": strength}), notice).verdict == Verdict.MATCH


@pytest.mark.parametrize("strength", ["250mg", "1500mg", "500mcg", "500mg/5ml"])
def test_different_strength_or_unit_does_not_match(
    medicine: Medicine, notice: Notice, strength: str,
) -> None:
    modified = notice.model_copy(update={"product_description": f"DemoRelief {strength} tablets"})
    decision = match(medicine, modified)
    assert decision.verdict == Verdict.NO_MATCH
    assert decision.evidence["strength"]["matched"] is False


def test_concentration_matches_only_full_expression(medicine: Medicine, notice: Notice) -> None:
    medicine = medicine.model_copy(update={"strength": "250 mg / 5 ml"})
    notice = notice.model_copy(update={"product_description": "DemoRelief 250mg/5mL oral liquid"})
    assert match(medicine, notice).verdict == Verdict.MATCH
    assert match(medicine.model_copy(update={"strength": "250mg"}), notice).verdict == Verdict.NO_MATCH


def test_other_brand_does_not_match(medicine: Medicine, notice: Notice) -> None:
    notice = notice.model_copy(update={"product_description": "OtherBrand 500 mg tablets"})
    assert match(medicine, notice).verdict == Verdict.NO_MATCH


def test_short_brand_is_not_an_exact_substring_match(medicine: Medicine, notice: Notice) -> None:
    medicine = medicine.model_copy(update={"name": "AB"})
    notice = notice.model_copy(update={"product_description": "ABC 500 mg tablets"})
    assert match(medicine, notice).verdict == Verdict.NO_MATCH


@pytest.mark.parametrize("batch", ["SYN003", "SYN00", "SYN0019", "001", "syn001", "SYN-001"])
def test_same_brand_different_exact_batch_is_no_match(
    medicine: Medicine, notice: Notice, batch: str,
) -> None:
    decision = match(medicine.model_copy(update={"batch_no": batch}), notice)
    assert decision.verdict == Verdict.NO_MATCH
    assert decision.evidence["brand"]["matched"] is True
    assert decision.evidence["batch"]["matched"] is False


@pytest.mark.parametrize("batch, code_info", [
    ("SYN-001", "Batch no.: SYN-001"),
    ("SYN_001", "Lots: SYN_001; SYN_002"),
    ("SYN/001", "Batch: SYN/001"),
    ("0001", "Lot # 0001"),
    ("ALPHA", "Lot: ALPHA"),
    ("SYN001", "SYN001, SYN002"),
    ("SYN001", "Lot: SYN001 and SYN002. Expiration date: 2027-01-01"),
    ("SYN002", "Lot SYN001; Exp: 01/2027; Lot SYN002; Exp: 02/2027"),
])
def test_explicit_batch_formats(medicine: Medicine, notice: Notice, batch: str, code_info: str) -> None:
    medicine = medicine.model_copy(update={"batch_no": batch})
    notice = notice.model_copy(update={"code_info": code_info})
    assert match(medicine, notice).verdict == Verdict.MATCH


def test_expiration_and_ndc_are_not_batch_identifiers(medicine: Medicine, notice: Notice) -> None:
    medicine = medicine.model_copy(update={"batch_no": "2027"})
    notice = notice.model_copy(update={"code_info": "NDC: 2027; Lot SYN001; Exp: 2027"})
    assert match(medicine, notice).verdict == Verdict.NO_MATCH


@pytest.mark.parametrize("batch", [None, "", "   "])
def test_missing_batch_requires_user_input(medicine: Medicine, notice: Notice, batch: str | None) -> None:
    decision = match(medicine.model_copy(update={"batch_no": batch}), notice)
    assert decision.verdict == Verdict.UNCERTAIN
    assert decision.evidence["requires_user_input"] is True
    assert decision.evidence["missing_fields"] == ["batch_no"]
    assert "batch number" in decision.evidence["question"]
    assert "pause" in decision.reason


@pytest.mark.parametrize("code_info", [
    None, "", "???", "Not available", "See attached notice", "All lots",
    "Lots A001 through A010", "Lots A001-A010", "SYN*", "Expiration: 2027",
    "unreadable source text", "Lot: not reported",
])
def test_missing_or_unparseable_batch_evidence_is_uncertain(
    medicine: Medicine, notice: Notice, code_info: str | None,
) -> None:
    decision = match(medicine, notice.model_copy(update={"code_info": code_info}))
    assert decision.verdict == Verdict.UNCERTAIN
    assert decision.evidence["batch"]["matched"] is None


@pytest.mark.parametrize("description", [
    None, "", "???", "unreadable text", "DemoRelief unknown strength",
    "DemoRelief 500mg/unknown", "DemoRelief 500mg2", "500mg",
    "DemoRelief 1,500mg", "DemoRelief -500mg",
    "DemoRelief 250mg; OtherBrand 500mg", "DemoRelief 500mg and 250mg tablets",
])
def test_unparseable_or_ambiguous_description_is_uncertain(
    medicine: Medicine, notice: Notice, description: str | None,
) -> None:
    decision = match(medicine, notice.model_copy(update={"product_description": description}))
    assert decision.verdict == Verdict.UNCERTAIN


def test_unknown_evidence_takes_precedence_over_mismatch(medicine: Medicine, notice: Notice) -> None:
    medicine = medicine.model_copy(update={"name": "OtherBrand"})
    notice = notice.model_copy(update={"code_info": None})
    assert match(medicine, notice).verdict == Verdict.UNCERTAIN


@pytest.mark.parametrize("threshold", [0, -1, 101, float("nan"), float("inf")])
def test_invalid_threshold_is_rejected(medicine: Medicine, notice: Notice, threshold: float) -> None:
    with pytest.raises(ValueError, match="brand_threshold"):
        match(medicine, notice, brand_threshold=threshold)


def test_existing_orm_instances_are_supported(medicine: Medicine, notice: Notice) -> None:
    medicine_row = MedicineRecord(**medicine.model_dump())
    notice_row = NoticeRecord(
        notice_id=notice.recall_number, url=str(notice.url), report_date=notice.report_date,
        product_description=notice.product_description, code_info=notice.code_info,
    )
    assert match(medicine_row, notice_row).verdict == Verdict.MATCH


def test_normalization_collapses_word_boundaries() -> None:
    assert normalize_name("  Demo-Relief®  ") == "demo relief"


def test_batch_leading_zeros_are_significant(medicine: Medicine, notice: Notice) -> None:
    medicine = medicine.model_copy(update={"batch_no": "001"})
    notice = notice.model_copy(update={"code_info": "Lot 0001"})
    assert match(medicine, notice).verdict == Verdict.NO_MATCH


def test_punctuation_in_multiword_brand(medicine: Medicine, notice: Notice) -> None:
    medicine = medicine.model_copy(update={"name": "Demo-Relief"})
    notice = notice.model_copy(update={"product_description": "Demo Relief 500mg tablets"})
    assert match(medicine, notice).verdict == Verdict.MATCH


def test_unparseable_household_batch_is_uncertain(medicine: Medicine, notice: Notice) -> None:
    medicine = medicine.model_copy(update={"batch_no": "SYN001 or SYN002"})
    assert match(medicine, notice).verdict == Verdict.UNCERTAIN
