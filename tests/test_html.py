import hashlib

import pytest

from panda.detectors import DetectorEngine, default_detector_engine
from panda.handlers.html import extract_html_markup_units, sanitise_html, sanitise_html_file
from panda.model import PIIKind
from panda.pseudonyms import FixedPseudonymiser


def _run(tmp_path, html):
    source = tmp_path / "in.html"
    source.write_text(html, encoding="utf-8")
    destination = tmp_path / "out.html"
    result = sanitise_html_file(
        source, destination, FixedPseudonymiser(), default_detector_engine()
    )
    return destination.read_text(encoding="utf-8"), result


def test_replaces_visible_text(tmp_path):
    text, result = _run(tmp_path, "<html><body><p>email real.person@mail.org</p></body></html>")
    assert "real.person@mail.org" not in text
    assert "john.doe@example.com" in text
    assert [r.detection.kind for r in result.replacements] == [PIIKind.EMAIL]


def test_comments_are_removed(tmp_path):
    """Hidden comments have no retained surface in the review dialect."""
    text, _ = _run(tmp_path, "<html><body><!-- ring 0298761234 --></body></html>")
    assert "0298761234" not in text
    assert "<!--" not in text and "-->" not in text


def test_attributes_are_removed(tmp_path):
    text, _ = _run(tmp_path, '<html><body><p class="a b c">hi</p></body></html>')
    assert "class" not in text
    assert "hi" in text


def test_no_pii_leaves_the_file_unchanged_in_substance(tmp_path):
    text, result = _run(tmp_path, "<html><body><p>nothing to see</p></body></html>")
    assert result.replacements == []
    assert "nothing to see" in text


@pytest.mark.parametrize(
    "surface",
    [
        '<img src="data:image/png;base64,SECRET">',
        '<picture><source srcset="SECRET"><img src="SECRET"></picture>',
        '<svg><text>SECRET</text><image href="SECRET"/></svg>',
        "<style>p { background: url(SECRET) }</style>",
        "<script>SECRET</script>",
        '<object data="SECRET">SECRET</object>',
        '<iframe srcdoc="SECRET">SECRET</iframe>',
        '<embed src="SECRET">',
        "<canvas>SECRET</canvas>",
        '<video poster="SECRET">SECRET</video>',
        '<audio src="SECRET">SECRET</audio>',
        "<math><mi>SECRET</mi></math>",
        "<template><p>SECRET</p></template>",
        "<!--SECRET-->",
        '<link href="SECRET">',
        '<meta content="SECRET">',
        '<!DOCTYPE html SYSTEM "SECRET">',
    ],
)
def test_removes_unverified_surfaces_and_preserves_source(tmp_path, surface):
    source = tmp_path / "source.html"
    source.write_text("<p>before</p>" + surface + "<p>after</p>")
    digest = hashlib.sha256(source.read_bytes()).digest()
    destination = tmp_path / "output.html"
    result = sanitise_html_file(source, destination, FixedPseudonymiser(), DetectorEngine())
    assert hashlib.sha256(source.read_bytes()).digest() == digest
    assert b"SECRET" not in destination.read_bytes()
    assert [u.text for u in extract_html_markup_units(result.text)] == ["before", "after"]


@pytest.mark.parametrize(
    "markup",
    [
        "<form><label>Name</label> <input value='Lena Fischer'></form>",
        "<custom-field>Lena Fischer</custom-field>",
        "<fieldset><legend>Contact</legend>Lena Fischer</fieldset>",
    ],
)
def test_form_and_unknown_element_text_is_kept_and_sanitised(markup):
    """Web-form exports used to vanish entirely because <form> was an unknown element."""
    result = sanitise_html(
        "<p>Name: x</p>" + markup, FixedPseudonymiser(), default_detector_engine()
    )
    assert "Lena" not in result.text and "Fischer" not in result.text
    assert "John Doe" in result.text


def test_hidden_and_password_inputs_are_dropped():
    result = sanitise_html(
        "<input type='hidden' value='SECRET'><input type='password' value='SECRET'>",
        FixedPseudonymiser(), DetectorEngine(),
    )
    assert "SECRET" not in result.text


def test_removes_all_attributes_and_client_controlled_names():
    result = sanitise_html(
        '<p secret.person@example.org="x" style="background:url(SECRET)" '
        'onclick="SECRET" title="SECRET" id="SECRET">safe</p>',
        FixedPseudonymiser(),
        DetectorEngine(),
    )
    assert result.text == "<html><body><p>safe</p></body></html>"
    assert "SECRET" not in repr(result.replacements)
    assert "secret.person" not in repr(result.replacements)


@pytest.mark.parametrize(
    "markup,expected",
    [
        ("<p>reach real.<b>person</b>&#64;<span>mail.org</span></p>", "reach john.doe@example.com"),
        ("<p>call 0412<span>345</span>678</p>", "call 0400000000"),
        ("<p>real.<!-- hidden --><i>person</i>@mail.org</p>", "john.doe@example.com"),
        ("<div>one <b>two</b><p>three</p>four<br>five</div>", None),
    ],
)
def test_inline_detection_and_reading_order(markup, expected):
    result = sanitise_html(markup, FixedPseudonymiser(), DetectorEngine())
    texts = [u.text for u in extract_html_markup_units(result.text)]
    assert texts == ([expected] if expected else ["one two", "three", "four", "five"])


@pytest.mark.parametrize(
    "markup",
    [
        '<html><body><p style="background:url(x)">safe</p></body></html>',
        '<html><body><p><img src="x"></p></body></html>',
        "<html><body><svg></svg></body></html>",
        "<html><body><script></script></body></html>",
        "<html><body><!--hidden--><p>safe</p></body></html>",
        "<html><body><p>real.<b>person</b>@mail.org</p></body></html>",
        "<html><body><p>&#115;afe</p></body></html>",
        "<html><body><p>safe</p></body></html><secret/>",
        "<html><body><p>%73afe</p></body></html>",
        "<html><body><p>safe</body></html>",
        b"<html><body><p>\xff</p></body></html>",
    ],
)
def test_checker_rejects_crafted_output_independently(markup):
    with pytest.raises((ValueError, UnicodeError)):
        extract_html_markup_units(markup)


@pytest.mark.parametrize("value", ["real%40mail.org", r"real\x40mail.org", r"real\u0040mail.org"])
def test_refuses_unsupported_visible_encodings(value):
    with pytest.raises(ValueError, match="Unsupported HTML text encoding"):
        sanitise_html("<p>" + value + "</p>", FixedPseudonymiser(), DetectorEngine())


def test_utf8_and_literal_markup_round_trip():
    result = sanitise_html(
        "<p>普通文本 &amp; &lt;img&gt;</p>", FixedPseudonymiser(), DetectorEngine()
    )
    assert extract_html_markup_units(result.text.encode())[0].text == "普通文本 & <img>"
