import json
from pathlib import Path

import pytest

from panda.core import sanitise_text
from panda.detectors import default_detector_engine
from panda.model import PIIKind
from panda.pseudonyms import DummyNames, FixedPseudonymiser
from panda.report import build_report

FIXTURES = Path(__file__).parent / "fixtures"

# Values that must not survive sanitising. Kept next to the fixtures they come from.
EN_SECRETS = [
    "Margaret Whitfield",
    "m.whitfield@example.com",
    "0412 908 771",
    "14/07/1968",
    "42 Kembla Street",
    "ACC-5540912",
    "02 4225 1180",
]
ZH_SECRETS = [
    "张伟",
    "李秀英",
    "zhang.wei@example.com",
    "13809871234",
    "1968年7月14日",
    "310101196807141237",
    "世纪大道",
    "13512340987",
]


@pytest.fixture
def pseudonymiser():
    return FixedPseudonymiser()


@pytest.fixture(scope="module")
def detector_engine():
    return default_detector_engine()


def _fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class TestReplacement:
    def test_replaces_a_single_value(self, pseudonymiser):
        result = sanitise_text("call 0412908771 today", pseudonymiser)
        assert "0412908771" not in result.text
        assert result.text.startswith("call ") and result.text.endswith(" today")

    def test_untouched_text_is_preserved_exactly(self, pseudonymiser):
        text = "no personal data of any kind appears in this sentence"
        assert sanitise_text(text, pseudonymiser).text == text

    def test_multiple_values_all_replaced(self, pseudonymiser):
        text = "0412908771 and m.whitfield@example.com and DOB 14/07/1968"
        result = sanitise_text(text, pseudonymiser)
        assert len(result.replacements) == 3
        for original in ("0412908771", "m.whitfield@example.com", "14/07/1968"):
            assert original not in result.text

    def test_spans_stay_aligned_with_fullwidth_input(self, pseudonymiser):
        """The classic off-by-one: detect on folded text, replace in the original."""
        result = sanitise_text("电话１３８０９８７１２３４结束", pseudonymiser)
        assert result.text.startswith("电话")
        assert result.text.endswith("结束")
        assert "１３８０９８７１２３４" not in result.text


class TestSurrogateShape:
    def test_generated_email_uses_a_reserved_domain(self, pseudonymiser):
        """RFC 2606 domains cannot resolve, so a dummy address can never be mailed."""
        assert pseudonymiser.surrogate(PIIKind.EMAIL, "a@b.com").endswith("@example.com")

    def test_chinese_name_gets_a_chinese_replacement(self, pseudonymiser):
        surrogate = pseudonymiser.surrogate(PIIKind.NAME, "张伟")
        assert all("一" <= c <= "鿿" for c in surrogate)

    def test_latin_name_gets_a_latin_replacement(self, pseudonymiser):
        assert pseudonymiser.surrogate(PIIKind.NAME, "Margaret Whitfield").isascii()


class TestEnglishFixture:
    def test_no_secret_survives(self, pseudonymiser, detector_engine):
        result = sanitise_text(_fixture("en/chat_transcript.txt"), pseudonymiser, detector_engine)
        for secret in EN_SECRETS:
            assert secret not in result.text, f"{secret} survived sanitising"

    def test_structure_is_preserved(self, pseudonymiser, detector_engine):
        source = _fixture("en/chat_transcript.txt")
        result = sanitise_text(source, pseudonymiser, detector_engine)
        assert result.text.count("\n") == source.count("\n")
        assert result.text.count("[2025-03-04") == source.count("[2025-03-04")

    def test_conversation_remains_followable(self, detector_engine):
        """Margaret is named twice and must be the same person both times."""
        result = sanitise_text(_fixture("en/chat_transcript.txt"), DummyNames(), detector_engine)
        names = {r.surrogate for r in result.replacements if r.detection.kind is PIIKind.NAME}
        assert len(names) == 2  # Margaret and Susan, not one per mention


class TestChineseFixture:
    def test_no_secret_survives(self, pseudonymiser, detector_engine):
        result = sanitise_text(_fixture("zh/chat_transcript.txt"), pseudonymiser, detector_engine)
        for secret in ZH_SECRETS:
            assert secret not in result.text, f"{secret} survived sanitising"

    def test_structure_is_preserved(self, pseudonymiser, detector_engine):
        source = _fixture("zh/chat_transcript.txt")
        result = sanitise_text(source, pseudonymiser, detector_engine)
        assert result.text.count("\n") == source.count("\n")
        assert result.text.count("客服：") == source.count("客服：")

    def test_name_is_caught_automatically(self, pseudonymiser):
        result = sanitise_text("请问是张伟先生吗", pseudonymiser)
        assert "张伟" not in result.text

    def test_address_is_caught_automatically(self, pseudonymiser):
        result = sanitise_text("地址是上海市浦东新区世纪大道100号", pseudonymiser)
        assert "世纪大道" not in result.text


class TestDeterminism:
    """Phase exit criterion: running twice must give byte-identical output."""

    @pytest.mark.parametrize("fixture", ["en/chat_transcript.txt", "zh/chat_transcript.txt"])
    def test_repeated_runs_are_identical(self, fixture, detector_engine):
        source = _fixture(fixture)
        first = sanitise_text(source, DummyNames(), detector_engine).text
        second = sanitise_text(source, DummyNames(), detector_engine).text
        assert first == second

    def test_detector_identity_is_stable(self, detector_engine):
        assert detector_engine.identity == detector_engine.identity


class TestReportSafety:
    """Phase exit criterion: a report must never contain raw values."""

    @pytest.mark.parametrize(
        "fixture,secrets",
        [
            ("en/chat_transcript.txt", EN_SECRETS),
            ("zh/chat_transcript.txt", ZH_SECRETS),
        ],
    )
    def test_no_raw_value_reaches_the_report(self, fixture, secrets, detector_engine):
        result = sanitise_text(_fixture(fixture), FixedPseudonymiser(), detector_engine)
        serialised = json.dumps(build_report("file-0001", result), ensure_ascii=False)
        for secret in secrets:
            assert secret not in serialised, f"{secret} leaked into the report"

    def test_report_counts_match_replacements(self, pseudonymiser, detector_engine):
        result = sanitise_text(_fixture("en/chat_transcript.txt"), pseudonymiser, detector_engine)
        report = build_report("file-0001", result)
        assert report["total"] == len(result.replacements)
        assert sum(report["counts"].values()) == report["total"]
