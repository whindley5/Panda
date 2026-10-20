import os
import subprocess
import sys

import pytest

from panda.detectors import (
    DetectorEngine,
    _cn_id_is_valid,
    detect_ner,
    detect_patterns,
    is_organisation,
    organisation,
    resolve_overlaps,
)
from panda.model import Confidence, Detection, PIIKind


def kinds_found(text):
    return {detection.kind for detection in detect_patterns(text)}


def matched(text, kind):
    return [d.text for d in resolve_overlaps(detect_patterns(text)) if d.kind is kind]


class TestEmail:
    def test_plain(self):
        assert matched("write to m.whitfield@example.com today", PIIKind.EMAIL) == [
            "m.whitfield@example.com"
        ]

    def test_not_confused_by_trailing_punctuation(self):
        assert matched("邮箱是 zhang.wei@example.com。", PIIKind.EMAIL) == ["zhang.wei@example.com"]


class TestPhone:
    @pytest.mark.parametrize(
        "text",
        ["0412 908 771", "0412908771", "+61412908771", "02 4225 1180", "0424251180"],
    )
    def test_australian_forms(self, text):
        assert matched(f"call {text} now", PIIKind.PHONE) == [text]

    @pytest.mark.parametrize("text", ["13809871234", "+8613809871234"])
    def test_chinese_forms(self, text):
        assert matched(f"电话 {text} 。", PIIKind.PHONE) == [text]

    def test_fullwidth_digits_are_found(self):
        """Chinese exports use full-width digits, and missing them is a silent leak."""
        found = detect_patterns("电话１３８０９８７１２３４。")
        assert [d.kind for d in found] == [PIIKind.PHONE]
        assert found[0].text == "１３８０９８７１２３４"

    def test_long_digit_run_is_not_a_phone(self):
        assert PIIKind.PHONE not in kinds_found("reference 04129087719999")


class TestDate:
    def test_slash_form(self):
        assert matched("born 14/07/1968 in", PIIKind.DOB) == ["14/07/1968"]

    def test_han_form(self):
        assert matched("1968年7月14日出生", PIIKind.DOB) == ["1968年7月14日"]

    def test_ambiguous_ordering_with_birth_context_matches(self):
        """04/03/1990 is a real date read either way when labelled as a DOB."""
        assert matched("DOB 04/03/1990", PIIKind.DOB) == ["04/03/1990"]

    def test_unlabelled_appointment_date_is_not_treated_as_a_dob(self):
        assert matched("appointment 04/03/1990", PIIKind.DOB) == []

    def test_impossible_date_is_rejected(self):
        assert matched("45/99/1990", PIIKind.DOB) == []

    def test_year_outside_plausible_range_is_rejected(self):
        assert matched("12/01/1023", PIIKind.DOB) == []


class TestChineseId:
    def test_valid_checksum_is_accepted(self):
        assert _cn_id_is_valid("310101196807141237")
        assert matched("证件号码 310101196807141237。", PIIKind.ID_NUMBER) == ["310101196807141237"]

    def test_invalid_checksum_is_rejected(self):
        """An 18 digit reference number must not be mistaken for an ID."""
        assert not _cn_id_is_valid("310101196807141234")
        assert matched("编号 310101196807141234。", PIIKind.ID_NUMBER) == []

    def test_trailing_x_is_accepted(self):
        assert _cn_id_is_valid("11010119900307395X")


def test_labelled_passport_offsets_confidence_and_exact_email_precedence():
    engine = DetectorEngine()
    text = 'Passport number: Ｎ１２３４５６７.'
    detection, = engine.detect(text)
    assert (detection.start, detection.end) == (17, 25)
    assert detection.text == text[17:25]
    assert detection.kind == PIIKind.ID_NUMBER
    assert detection.detector == 'pattern.id.labelled'
    assert detection.confidence == Confidence.PATTERN and detection.score is None
    detection, = engine.detect('Passport number: N1234567@example.org')
    assert detection.kind == PIIKind.EMAIL
    assert detection.confidence == Confidence.VALIDATED


def test_passport_shape_needs_label_and_complete_token():
    for text in ['N1234567', 'Passport number: N1234567Z', 'Passport number: N12345']:
        assert PIIKind.ID_NUMBER not in kinds_found(text)
    # Labelled document numbers of other lengths are still identifiers.
    for text in ['Passport number: N123456', 'Passport number: N123456789']:
        assert PIIKind.ID_NUMBER in kinds_found(text)


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("My TFN is 123 456 782 if needed", "123 456 782"),
        ("Medicare card: 2123 45670 1", "2123 45670 1"),
        ("Nationality / passport: Australian / PA7654321", "PA7654321"),
        ("Driver licence number: S1234567 (SA)", "S1234567"),
    ],
)
def test_government_identifiers(text, value):
    assert matched(text, PIIKind.ID_NUMBER) == [value]


@pytest.mark.parametrize(
    ("text", "value"),
    [("微信号 wxid_wangwei85", "wxid_wangwei85"), ("WeChat: chenyu_hz)", "chenyu_hz"),
     ("Customer number: 4400 1188", "4400 1188")],
)
def test_messaging_and_customer_identifiers(text, value):
    assert matched(text, PIIKind.ACCOUNT) == [value]


def test_generic_reference_is_not_an_account_without_account_evidence():
    assert matched('Reference: 12345678', PIIKind.ACCOUNT) == []
    assert matched('Account reference: 12345678', PIIKind.ACCOUNT) == ['12345678']
    assert matched('Reference: ACC-12345678', PIIKind.ACCOUNT) == ['ACC-12345678']


class TestAccount:
    def test_labelled_account_is_found(self):
        assert matched("the account ending 8823 4417 is", PIIKind.ACCOUNT) == ["8823 4417"]

    def test_chinese_label_is_found(self):
        assert matched("账号尾号 8823 4417。", PIIKind.ACCOUNT) == ["8823 4417"]

    def test_unlabelled_digits_are_left_alone(self):
        """Flagging every digit run would make the output unreadable."""
        assert matched("total was 4417 dollars", PIIKind.ACCOUNT) == []

    def test_label_itself_is_not_replaced(self):
        found = [d for d in detect_patterns("account 55409127") if d.kind is PIIKind.ACCOUNT]
        assert found[0].text == "55409127"

    def test_trailing_whitespace_is_not_part_of_the_number(self):
        """The greedy digit class eats a trailing newline; the span must not.

        Left in, the span would cover the line break and the replacement would delete
        it, and the reported value would not match a known dummy on the leak rescan.
        """
        found = [d for d in detect_patterns("account 55409127\nnext") if d.kind is PIIKind.ACCOUNT]
        assert found[0].text == "55409127"
        assert found[0].end == len("account 55409127")


class TestNer:
    def test_detects_a_chinese_name(self):
        (found,) = detect_ner("请问是张伟先生吗")
        assert found.kind is PIIKind.NAME
        assert found.text == "张伟"
        assert found.confidence is Confidence.NER
        assert found.detector == "ner.name"

    def _addresses(self, text):
        return [d for d in detect_ner(text) if d.kind is PIIKind.ADDRESS]

    def test_detects_a_street_address(self):
        found = self._addresses("地址是上海市浦东新区世纪大道100号")
        assert [d.text for d in found] == ["上海市浦东新区世纪大道100号"]

    def test_address_span_maps_back_to_the_original_fullwidth_text(self):
        """Segmentation runs on folded text; the span must recover the real characters."""
        (found,) = self._addresses("上海市浦东新区世纪大道１００号")
        assert "１００" in found.text and found.text.endswith("号")

    def test_stale_pkg_resources_stub_does_not_break_jieba(self, tmp_path):
        """An empty pkg_resources directory imports but has no resource_stream."""
        (tmp_path / "pkg_resources").mkdir()
        code = ("from panda.detectors import detect_ner\n"
                "print(detect_ner('请问是张伟先生吗')[0].text)")
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(
            [str(tmp_path), *filter(None, [os.environ.get("PYTHONPATH")])])}
        result = subprocess.run([sys.executable, "-c", code], env=env,
                                capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stderr[-2000:]
        assert result.stdout.strip() == "张伟"

    def test_bare_city_mention_is_not_an_address(self):
        """Without a street-level suffix it is a locality, not worth redacting."""
        assert self._addresses("我在上海工作") == []

    def test_english_text_is_left_to_the_other_layers(self):
        """NER is Chinese-only; running the segmenter on English earns nothing."""
        assert detect_ner("posted to 42 Kembla Street, Wollongong NSW 2500") == []

    def test_ner_loses_to_a_pattern_on_the_same_span(self):
        """A guessed name overlapping a validated email must not win."""
        ner = Detection(PIIKind.NAME, 0, 5, "aaaaa", "ner.name", Confidence.NER)
        email = Detection(PIIKind.EMAIL, 0, 5, "aaaaa", "pattern.email", Confidence.VALIDATED)
        assert resolve_overlaps([ner, email]) == [email]


class TestOverlapResolution:
    def _detection(self, start, end, confidence=Confidence.PATTERN, kind=PIIKind.NAME):
        return Detection(kind, start, end, "x" * (end - start), "test", confidence)

    def test_higher_confidence_decides_kind_over_the_whole_overlap(self):
        weak = self._detection(0, 10, Confidence.PATTERN, PIIKind.ADDRESS)
        strong = self._detection(2, 6, Confidence.VALIDATED)
        (merged,) = resolve_overlaps([weak, strong])
        assert (merged.kind, merged.start, merged.end) == (PIIKind.NAME, 0, 10)

    def test_partial_overlap_replaces_the_union(self):
        first = self._detection(0, 6)
        second = self._detection(4, 12, Confidence.NER)
        (merged,) = resolve_overlaps([first, second])
        assert (merged.start, merged.end, merged.confidence) == (0, 12, Confidence.PATTERN)

    def test_longer_span_wins_at_equal_confidence(self):
        short = self._detection(0, 4)
        long = self._detection(0, 10)
        assert resolve_overlaps([short, long]) == [long]

    def test_non_overlapping_are_all_kept(self):
        first = self._detection(0, 4)
        second = self._detection(5, 9)
        assert resolve_overlaps([second, first]) == [first, second]

    def test_result_is_ordered_by_position(self):
        first = self._detection(0, 4)
        second = self._detection(10, 14)
        third = self._detection(20, 24)
        assert resolve_overlaps([third, first, second]) == [first, second, third]

    def test_id_number_beats_the_date_inside_it(self):
        """A Chinese ID embeds a birth date, and the ID must win."""
        text = "证件号码 310101196807141237。"
        kept = resolve_overlaps(detect_patterns(text))
        assert [d.kind for d in kept] == [PIIKind.ID_NUMBER]


class TestDetectorEngine:
    def test_runs_patterns_and_configured_providers_together(self):
        class SyntheticProvider:
            identity = "synthetic-provider"

            def detect(self, text):
                return [
                    Detection(
                        PIIKind.NAME,
                        0,
                        18,
                        text[:18],
                        "synthetic.name",
                        Confidence.NER,
                    )
                ]

        engine = DetectorEngine((SyntheticProvider(),))
        found = engine.detect("Margaret Whitfield: 0412908771")
        assert [item.kind for item in found] == [PIIKind.NAME, PIIKind.PHONE]
        assert engine.identity == (
            "patterns.au-cn.v2",
            "jieba.zh.v1",
            "synthetic-provider",
        )


class TestOrganisation:
    def test_nothing_is_kept_by_default(self):
        assert not is_organisation("Crane Brokers")

    @pytest.mark.parametrize("value", [
        "Crane", "Crane Brokers", "CRANE TRADER", "Crane Client Services", "白鹤证券（澳大利亚）",
    ])
    def test_terms_cover_business_names_built_on_them(self, value):
        with organisation(keep=["Crane Brokers", "白鹤"]):
            assert is_organisation(value)

    @pytest.mark.parametrize("value", ["Crane Zhang", "Brokers", "Mia Crane"])
    def test_a_term_next_to_a_name_is_still_a_name(self, value):
        with organisation(keep=["Crane Brokers"]):
            assert not is_organisation(value)

    def test_account_prefixes_are_configurable(self):
        text = "Ref CB-43021998 and ACC-5540912"
        assert matched(text, PIIKind.ACCOUNT) == ["ACC-5540912"]
        with organisation(account_prefixes=["CB-"]):
            assert matched(text, PIIKind.ACCOUNT) == ["CB-43021998", "ACC-5540912"]
