from types import SimpleNamespace

import pytest

from panda.model import Confidence, PIIKind
from panda.spacy_recognizer import SpacyRecognizer


@pytest.mark.parametrize(("text", "label", "expected"), [
    ("Dr Margaret Whitfield", "PERSON", "Margaret Whitfield"),
    ("John Doe:", "PERSON", "John Doe"),
    ("Margaret Whitfield’s", "PERSON", "Margaret Whitfield"),
    ("Margaret Whitfield\nAccount", "PERSON", "Margaret Whitfield"),
    ("Margaret Whitfield\r\nAccount", "PERSON", "Margaret Whitfield"),
    ("J. Smith", "PERSON", "J. Smith"),
    ("Anne-Marie Smith", "PERSON", "Anne-Marie Smith"),
    # Han is masked for spaCy; jieba owns 王伟, and the person span keeps the English name.
    ("王伟 met Margaret Whitfield", "PERSON", "Margaret Whitfield"),
    ("42 Example Street", "GPE", "42 Example Street"),
    ("42 Example Street", "LOC", "42 Example Street"),
    ("Sydney", "GPE", None),
    ("Example Street", "ORG", None),
    ("Example Street", "FAC", None),
    ("\nAccount", "PERSON", None),
    ("Dr ", "PERSON", None),
])
def test_entity_mapping_cleanup_and_provenance(text, label, expected):
    provider = SpacyRecognizer()
    provider._nlp = lambda value: SimpleNamespace(ents=[
        SimpleNamespace(start_char=0, end_char=len(value), label_=label),
    ])
    detections = provider.detect(text)
    if expected is None:
        assert detections == []
        return
    detection, = detections
    assert detection.text == expected
    assert text[detection.start:detection.end] == expected
    assert detection.detector == f"spacy.{label.lower()}"
    assert detection.confidence == Confidence.NER
    assert detection.score is None
    assert detection.kind == (PIIKind.NAME if label == "PERSON" else PIIKind.ADDRESS)


def test_model_loads_once_and_does_not_load_for_chinese_only(monkeypatch):
    import spacy

    calls = []

    class Pipeline:
        pipe_names = ["ner"]

        def __call__(self, text):
            return SimpleNamespace(ents=[])

    def load(name):
        calls.append(name)
        return Pipeline()

    monkeypatch.setattr(spacy, "load", load)
    provider = SpacyRecognizer()
    assert provider.detect("王伟") == []
    assert calls == []
    provider.detect("hello")
    provider.detect("another fragment")
    assert calls == ["en_core_web_md"]


def test_missing_model_fails_without_download():
    with pytest.raises(RuntimeError, match="not installed"):
        SpacyRecognizer(model_name="synthetic_missing_model").detect("hello")


def test_pipeline_without_ner_fails_closed(monkeypatch):
    import spacy

    monkeypatch.setattr(spacy, "load", lambda name: SimpleNamespace(pipe_names=[]))
    with pytest.raises(RuntimeError, match="no entity recogniser"):
        SpacyRecognizer().detect("hello")


def test_real_provider_preserves_offsets_and_has_no_presidio_dependency(monkeypatch):
    import builtins

    original = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.startswith("presidio"):
            raise AssertionError("production must not import Presidio")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    provider = SpacyRecognizer()
    text = "Please call Margaret Whitfield."
    result, = provider.detect(text)
    assert result.text == "Margaret Whitfield"
    assert text[result.start:result.end] == result.text
    assert provider.identity.startswith("spacy-")
    assert "threshold" not in provider.identity


def test_prepare_loads_model_before_any_text_and_reuses_it(monkeypatch):
    import spacy

    calls = []
    monkeypatch.setattr(
        spacy, 'load', lambda name: calls.append(name) or SimpleNamespace(pipe_names=['ner'])
    )
    provider = SpacyRecognizer()
    provider.prepare()
    provider.prepare()
    assert calls == ['en_core_web_md']


def test_incompatible_version_refused_before_model_load(monkeypatch):
    import spacy

    import panda.spacy_recognizer as module

    monkeypatch.setattr(module, 'version', lambda name: '0.0.0')
    monkeypatch.setattr(spacy, 'load', lambda name: pytest.fail('must validate before loading'))
    with pytest.raises(module.ModelSetupError, match='versions do not match'):
        SpacyRecognizer().prepare()


def test_broken_model_load_has_safe_exception_message(monkeypatch):
    import spacy

    from panda.spacy_recognizer import ModelSetupError

    def broken(name):
        raise OSError('synthetic.private@example.org')

    monkeypatch.setattr(spacy, 'load', broken)
    with pytest.raises(ModelSetupError, match='could not be loaded') as error:
        SpacyRecognizer().prepare()
    assert 'synthetic.' not in str(error.value)
