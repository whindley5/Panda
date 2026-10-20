"""Regression tests for chat-export PII: speaker handles, cue names and propagation.

Every value here is synthetic. The transcript layout mirrors the customer-service
exports the tool is used on: ``[Role]handle YYYY-MM-DD HH:MM:SS`` then the message.
"""

import pytest

from panda import cli
from panda.detectors import (
    DetectorEngine,
    default_detector_engine,
    detect_patterns,
    organisation,
    resolve_overlaps,
)
from panda.model import PIIKind
from panda.propagation import KnownTermsRecognizer, collect_terms, name_terms
from panda.pseudonyms import ALL_FIXED_SURROGATES, DUMMY_NAMES
from test_pdf import _build_pdf


def found(text, kind=None):
    return [
        d.text for d in resolve_overlaps(detect_patterns(text))
        if kind is None or d.kind is kind
    ]


class TestSpeakerHandles:
    @pytest.mark.parametrize(
        "handle", ["NadiaK", "Nadia Kowalski", "nadia_k", "jenny_wu88", "娜娜"],
    )
    def test_customer_display_name_is_a_name(self, handle):
        text = f"[Customer]{handle} 2026-05-02 10:14:03\nHi there"
        assert found(text, PIIKind.NAME) == [handle]

    def test_customer_platform_id_is_an_identifier(self):
        text = "[Customer]8f3a91c2 2026-05-02 10:14:03\n你好"
        assert found(text, PIIKind.ACCOUNT) == ["8f3a91c2"]

    def test_header_glued_to_previous_line_is_found(self):
        text = "[agent]AU-0031 2026-05-02 10:15:10ok[Customer]NadiaK 2026-05-02 10:15:30\nyes"
        assert found(text, PIIKind.NAME) == ["NadiaK"]

    @pytest.mark.parametrize(
        "header", ["[agent]AU-0031", "[agent]01466", "[agent]system", "[AI]xi", "[system]Nadia"],
    )
    def test_staff_codes_and_automated_speakers_are_kept(self, header):
        assert found(f"{header} 2026-05-02 10:15:10\nHello") == []

    def test_phone_number_handle_is_left_to_the_phone_recogniser(self):
        detections = resolve_overlaps(
            detect_patterns("[Customer]0412345678 2026-05-02 10:14:03\nhi")
        )
        assert [(d.kind, d.text) for d in detections] == [(PIIKind.PHONE, "0412345678")]


class TestCueNames:
    @pytest.mark.parametrize(
        ("text", "name"),
        [
            ("Thank you Nadia~ Have a good day~", "Nadia"),
            ("Hi Priya, thanks for waiting", "Priya"),
            ("Thanks Omar Have a nice day", "Omar"),
            ("Dear Mr Okafor,", "Okafor"),
            ("My name is Lena Fischer and I need help", "Lena Fischer"),
            ("Name: LENA FISCHER", "LENA FISCHER"),
        ],
    )
    def test_names_after_cues(self, text, name):
        assert name in found(text, PIIKind.NAME)

    @pytest.mark.parametrize(
        "text", ["Hi there", "Hi team, quick question", "Dear Sir,", "Thanks Again", "Thank you"],
    )
    def test_generic_addressees_are_not_names(self, text):
        assert found(text, PIIKind.NAME) == []


class TestFalsePositives:
    def test_clock_time_before_please_is_not_an_address(self):
        """A regression: ``16\\nPlease`` was read as ``16 Pl(ace)`` and corrupted."""
        assert found("[agent]AU-0031 2026-05-02 07:13:16\nPlease share your details") == []

    def test_real_address_still_matches(self):
        assert found("I live at 42 Wattle Street, Kogarah NSW 2217", PIIKind.ADDRESS) == [
            "42 Wattle Street, Kogarah NSW 2217"
        ]

    def test_referral_code_needs_a_digit(self):
        assert found("I got a referral code from a friend") == []
        assert found("她的邀请码是：QX7731", PIIKind.ACCOUNT) == ["QX7731"]

    def test_brand_words_are_not_chinese_names(self):
        text = "您已登录白鹤账户，我是助理小鹤"
        names = [d.text for d in DetectorEngine().detect(text) if d.kind is PIIKind.NAME]
        assert names == ["白鹤"]
        with organisation(keep=["白鹤", "小鹤"]):
            detections = DetectorEngine().detect(text)
        assert [d for d in detections if d.kind is PIIKind.NAME] == []


class TestChineseHonorifics:
    @pytest.mark.parametrize("text", ["好的，王女士，", "李先生您好", "谢谢陈小姐"])
    def test_surname_plus_honorific(self, text):
        names = found(text, PIIKind.NAME)
        assert len(names) == 1 and names[0][0] in "王李陈"

    def test_thanks_is_not_swallowed(self):
        assert found("谢谢王先生", PIIKind.NAME) == ["王先生"]


class TestPropagation:
    def test_camel_case_handle_yields_first_name(self):
        assert {"NadiaK", "Nadia"} <= name_terms("NadiaK")

    def test_full_name_yields_parts_but_not_titles(self):
        terms = name_terms("Dr Lena Fischer")
        assert {"Lena", "Fischer"} <= terms and "Dr" not in terms

    def test_han_name_yields_given_name_and_honorific_forms(self):
        terms = name_terms("王晓明")
        assert {"王晓明", "晓明", "王先生", "王女士"} <= terms

    def test_known_terms_match_elsewhere_without_touching_dummies(self):
        engine = KnownTermsRecognizer(
            collect_terms(detect_patterns("[Customer]JohnP 2026-05-02 10:14:03\nhi")),
            exempt=ALL_FIXED_SURROGATES,
        )
        text = "Thanks John~ I have told John Doe"
        assert [d.text for d in engine.detect(text)] == ["John"]

    def test_fragments_match_case_forms_not_common_words(self):
        engine = KnownTermsRecognizer({PIIKind.NAME: name_terms("Grace Hope")})
        assert [d.text for d in engine.detect("Grace has hope, HOPE")] == ["Grace", "HOPE"]

    def test_stop_word_fragments_are_never_terms(self):
        assert "Will" not in name_terms("Will Barker")


def _chat_pdf(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    lines = [
        "[Customer]NadiaK 2026-05-02 10:14:03",
        "Hi team, I got a referral code from a friend.",
        "[agent]AU-0031 2026-05-02 07:13:16",
        "Please share your best contact details and your name please",
        "[Customer]NadiaK 2026-05-02 10:16:40",
        "0412 345 678. Nadia Kowalski",
        "[agent]AU-0031 2026-05-02 10:20:02",
        "Thank you Nadia~ Have a good day~",
        "[agent]system 2026-05-02 10:21:00",
        "The system sends an invitation satisfaction message",
    ]
    (source / "chat.pdf").write_bytes(_build_pdf([lines[:6], lines[6:]]))
    return source


def test_chat_pdf_end_to_end(tmp_path):
    source = _chat_pdf(tmp_path)
    output, reports = tmp_path / "out", tmp_path / "rep"
    args = ["run", str(source), "--opaque-names", "--output", str(output), "--report", str(reports)]
    assert cli.main(args) == 0
    text = (output / "file-0001.pdf.txt").read_text(encoding="utf-8")
    for leaked in ("Nadia", "Kowalski", "0412"):
        assert leaked not in text
    for kept in ("AU-0031", "07:13:16\nPlease", "[agent]system", "The system sends",
                 "referral code from a friend"):
        assert kept in text
    # The handle and the bare first name later in the chat are one dummy person.
    dummy = DUMMY_NAMES[0]
    assert text.count(f"[Customer]{dummy}") == 2
    assert f"Thank you {dummy}" in text


def test_verification_flags_a_surviving_known_name(tmp_path):
    """The rescan must not certify output where a name found elsewhere survived."""
    engine = default_detector_engine()
    engine.prepare()
    terms = collect_terms(detect_patterns("[Customer]NadiaK 2026-05-02 10:14:03\nhi"))
    engine = engine.with_provider(KnownTermsRecognizer(terms, exempt=ALL_FIXED_SURROGATES))
    assert any(d.text == "Nadia" for d in engine.detect("ok Nadia~ bye"))


class TestFormerlyKnownMisses:
    @pytest.mark.parametrize(
        "phone", ["0 2 9555 0184", "0 4 1234 5678", "0-2-9555-0184"],
    )
    def test_phone_with_spaced_area_code(self, phone):
        assert found(f"Call me on {phone} after five.", PIIKind.PHONE) == [phone]

    @pytest.mark.parametrize("text", ["ref 10 2 9555 0184", "0 2 9555 01845", "qty 0 2 3"])
    def test_spaced_area_code_needs_an_exact_phone_shape(self, text):
        assert found(text, PIIKind.PHONE) == []

    @pytest.mark.parametrize(("text", "alias"), [
        ("Park is his mother's surname; he sometimes signs J. Park", "J. Park"),
        ("my super fund shows me as R. Venkataraman.", "R. Venkataraman"),
        ("He signs as S.Y. Jung on transfers.", "S.Y. Jung"),
    ])
    def test_initial_and_surname_alias(self, text, alias):
        assert alias in found(text, PIIKind.NAME)

    def test_signed_as_a_verb_is_not_an_alias(self):
        assert found("She signed the form yesterday.", PIIKind.NAME) == []

    def test_lower_case_hyphenated_given_name(self):
        assert found("Legal name: Baek Hyun-woo", PIIKind.NAME) == ["Baek Hyun-woo"]

    @pytest.mark.parametrize(("value", "expected", "absent"), [
        ("Min-jun Jung", {"Minjun", "Min-jun", "Jung"}, {"jun", "Min", "Jun"}),
        ("Rajesh Venkataraman", {"Raj", "Rajesh"}, set()),
        ("Jonathan Smith", {"Jon", "Jonno"}, set()),
        ("Maria Lopez", {"Maria", "Lopez"}, {"Mar", "Lop"}),
        ("Vanessa Lo-Brennan", {"Lo-Brennan", "Brennan"}, {"Lobrennan"}),
    ])
    def test_propagated_name_forms(self, value, expected, absent):
        terms = name_terms(value)
        assert expected <= terms
        assert not absent & terms

    def test_nickname_and_joined_form_propagate(self):
        engine = DetectorEngine(providers=(KnownTermsRecognizer(
            {PIIKind.NAME: name_terms("Rajesh Venkataraman") | name_terms("Min-jun Jung")},
            exempt=ALL_FIXED_SURROGATES,
        ),))
        text = "Regards,\nRaj\nMinjun's is 0466 320 118. Rajasthan trip on 3 Mar."
        names = [d.text for d in engine.detect(text) if d.kind is PIIKind.NAME]
        assert names == ["Raj", "Minjun"]

    @pytest.mark.parametrize("address", [
        "Room 1502, Building 7, Jinjiang District, Chengdu, Sichuan 610021",
        "Flat B, 23/F, Tower 5, Taikoo Shing, Quarry Bay, Hong Kong",
        "No. 88 Tianfu Avenue, Gaoxin District, Chengdu 610041, China",
    ])
    def test_english_language_chinese_and_hk_addresses(self, address):
        assert found(f"Address: {address}. Thanks", PIIKind.ADDRESS) == [address]

    @pytest.mark.parametrize("text", [
        "We have offices in Sydney, Melbourne, Hong Kong and Singapore.",
        "Markets: ASX, NYSE, HKEX, Shanghai",
    ])
    def test_lists_of_places_are_not_addresses(self, text):
        assert found(text, PIIKind.ADDRESS) == []


def test_usernames_built_from_known_names_are_replaced():
    names = name_terms("Elijah Zimmerman") | name_terms("Melissa Hartley")
    recognizer = KnownTermsRecognizer({PIIKind.NAME: names})
    text = (
        "Logins: elijahzimmerman, e.zimmerman, ElijahZ, melissa_hartley1990, Melissa2023. "
        "Mail elijah.zimmerman@example.com or see example.org/in/elijah-zimmerman."
    )
    found = {d.text for d in recognizer.detect(text) if d.detector == "propagated.handle"}
    assert found == {
        "elijahzimmerman", "e.zimmerman", "ElijahZ", "melissa_hartley1990", "Melissa2023",
        "elijah-zimmerman",
    }


def test_ordinary_words_containing_known_names_are_not_handles():
    recognizer = KnownTermsRecognizer({PIIKind.NAME: name_terms("Grace Young")})
    text = "With grace and willingness, the Young-adult programme meets gracefully."
    assert not [d for d in recognizer.detect(text) if d.detector == "propagated.handle"]


@pytest.mark.parametrize("text, value", [
    ("The username is samuel.henderson. The password is hidden.", "samuel.henderson"),
    ("Username: dwhite, Contact Number", "dwhite"),
    ("The user name associated with this export is d.moreno. Next", "d.moreno"),
    ("Hi nikita.ivanov,\n\nWe received your request.", "nikita.ivanov"),
    ("Login: jh00123", "jh00123"),
    ("- **User Name**: PaytonFryer\n", "PaytonFryer"),
    ("The purchase was made using user name mb9983 and the product", "mb9983"),
    ("For example, the user name IslaSimmons must use a strong password.", "IslaSimmons"),
    ("All data, including user names, such as jason2023, must be handled", "jason2023"),
])
def test_cued_usernames_are_accounts(text, value):
    found = [d.text for d in detect_patterns(text) if d.kind is PIIKind.ACCOUNT]
    assert found == [value]


@pytest.mark.parametrize("text", [
    "Hi team, the username is required.",
    "Dear Dr. Smith, version 2.0 of the user id is not set.",
    "Username: N/A",
    "Enter your user name and password; the user name field is case sensitive.",
])
def test_username_cues_ignore_ordinary_words(text):
    assert not [d for d in detect_patterns(text) if d.kind is PIIKind.ACCOUNT]
