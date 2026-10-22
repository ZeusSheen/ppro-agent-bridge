"""Transcript JSON: the local format check, language codes, Whisper conversion (punctuation typed, NO_SPEECH),
reading files, word counts and the digest."""

import contextlib
import io
import json
import sys
import tempfile
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock

from helpers import BridgeError

from ppro_bridge import digest, transcribe, transcript

SPK = str(uuid.UUID(int=7, version=4))


def word(text, start, eos=False, kind="word", conf=0.9):
    return {"text": text, "start": start, "duration": 0.3, "eos": eos, "confidence": conf, "tags": [], "type": kind}


def doc(**over):
    d = {"language": "en-us", "speakers": [{"id": SPK, "name": "Speaker 1"}], "segments": [
        {"start": 0.0, "duration": 1.0, "language": "en-us", "speaker": SPK,
         "words": [word("Hello", 0.0), word("world", 0.3), word(".", 0.6, eos=True, kind="punctuation")]}]}
    d.update(over)
    return d


def tmp() -> Path:
    d = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(d.cleanup)
    return Path(d.name)


class ValidateTest(unittest.TestCase):
    def problems(self, d):
        return transcript.validate(d)

    def assert_problem(self, d, fragment):
        found = self.problems(d)
        self.assertTrue(any(fragment in p for p in found), (fragment, found))

    def test_valid(self):
        self.assertEqual(self.problems(doc()), [])
        self.assertEqual(self.problems(doc(language="??-??")), [])

    def test_top_level(self):
        self.assert_problem(doc(extra=1), "extra: unknown top-level key")
        d = doc()
        del d["speakers"]
        self.assert_problem(d, "speakers: required")
        self.assert_problem(doc(segments=[]), "at least one segment")
        self.assert_problem(doc(language="zh-cn"), "language")
        self.assert_problem(doc(language="en-US"), "language")  # the JSON codes are lowercase
        self.assertEqual(self.problems([]), ["transcript: expected a JSON object"])

    def test_segments(self):
        d = doc()
        del d["segments"][0]["duration"]
        self.assert_problem(d, "segments[0].duration: required")
        d = doc()
        d["segments"][0]["words"] = []
        self.assert_problem(d, "at least one word")
        d = doc()
        d["segments"][0]["language"] = "se-se"
        self.assert_problem(d, "segments[0].language")

    def test_speakers(self):
        self.assert_problem(doc(speakers=[{"id": "spk1", "name": "x"}]), "not a UUID v4")
        self.assert_problem(doc(speakers=[{"id": str(uuid.UUID(int=7, version=1)), "name": "x"}]), "UUID v4")
        other = str(uuid.UUID(int=8, version=4))
        self.assert_problem(doc(speakers=[{"id": other, "name": "x"}]), "not listed in speakers")

    def test_words(self):
        for change, fragment in (({"confidence": 1.5}, "confidence"), ({"confidence": True}, "confidence"),
                                 ({"tags": ["loud"]}, "tags"), ({"type": "noise"}, "type"),
                                 ({"eos": 1}, "eos"), ({"start": -1}, "start"), ({"text": 5}, "text")):
            d = doc()
            d["segments"][0]["words"][0].update(change)
            self.assert_problem(d, fragment)
        d = doc()
        del d["segments"][0]["words"][1]["eos"]
        self.assert_problem(d, "words[1].eos: required")
        d = doc()
        d["segments"][0]["words"][0]["tags"] = ["filler", "profanity"]
        self.assertEqual(self.problems(d), [])

    def test_count_words(self):
        self.assertEqual(transcript.count_words(doc()), 2)
        untyped = doc()
        del untyped["segments"][0]["words"][0]["type"]
        self.assertEqual(transcript.count_words(untyped), 2)
        self.assertEqual(transcript.count_words({}), 0)


class LanguageTest(unittest.TestCase):
    def test_thirty_codes(self):
        self.assertEqual(len(transcript.LANGUAGES), 30)
        self.assertEqual(len(set(transcript.LANGUAGES)), 30)
        self.assertIn("??-??", transcript.LANGUAGES)
        self.assertTrue(all(c == c.lower() for c in transcript.LANGUAGES))
        self.assertTrue(set(transcript.ADOBE_LANG.values()) <= set(transcript.LANGUAGES))

    def test_whisper_codes(self):
        for whisper, adobe in (("en", "en-us"), ("zh", "cmn-hans"), ("yue", "zh-hk"), ("sv", "sv-se"),
                               ("pt", "pt-br"), ("tl", "fil-ph"), ("id", "id-id"), ("pa", "pa-in"),
                               ("fi", "??-??"), ("ar", "??-??"), ("xx", "??-??"), (None, "??-??"), ("", "??-??")):
            self.assertEqual(transcript.adobe_language(whisper), adobe, whisper)

    def test_transcript_codes_pass_through(self):
        self.assertEqual(transcript.adobe_language("EN-GB"), "en-gb")
        self.assertEqual(transcript.adobe_language("cmn_Hant"), "cmn-hant")


class ReadTest(unittest.TestCase):
    def test_errors(self):
        d = tmp()
        with self.assertRaises(BridgeError) as cm:
            transcript.read_transcript(d / "missing.json")
        self.assertEqual(cm.exception.code, "FILE_NOT_FOUND")
        for name, data in (("bad.json", b"{not json"), ("list.json", b"[]"), ("latin.json", b"{\"a\": \"\xe9\"}")):
            (d / name).write_bytes(data)
            with self.assertRaises(BridgeError) as cm:
                transcript.read_transcript(d / name)
            self.assertEqual(cm.exception.code, "TRANSCRIPT_INVALID", name)

    def test_bom_is_fine(self):
        p = tmp() / "bom.json"
        p.write_bytes(b"\xef\xbb\xbf" + json.dumps(doc()).encode("utf-8"))
        text, parsed = transcript.read_transcript(p)
        self.assertFalse(text.startswith("\ufeff"))
        self.assertEqual(parsed, doc())


def segment(*words):
    return types.SimpleNamespace(words=[types.SimpleNamespace(word=w, start=s, end=e, probability=p)
                                        for w, s, e, p in words])


class ToAdobeTest(unittest.TestCase):
    def test_conversion_is_valid_and_types_punctuation(self):
        out = transcribe.to_adobe([segment((" Hello", 0.0, 0.4, 0.9), (" world.", 0.4, 0.8, 1.2), ("\u3002", 0.8, 0.8, 0.5)),
                                   segment((" ", 1.0, 1.1, 0.5))],
                                  "en-us", "Speaker 1")
        self.assertEqual(transcript.validate(out), [])
        words = out["segments"][0]["words"]
        self.assertEqual([w["type"] for w in words], ["word", "word", "punctuation"])
        self.assertEqual(words[1]["confidence"], 1.0)  # clamped
        self.assertEqual(len(out["segments"]), 1)  # the empty segment is dropped
        self.assertEqual(uuid.UUID(out["speakers"][0]["id"]).version, 4)

    def test_nothing_spoken(self):
        out = transcribe.to_adobe([segment((",", 0.0, 0.1, 0.5))], "en-us", "S")
        self.assertEqual((out["segments"], transcript.count_words(out)), ([], 0))


class FakeModel:
    segments = []

    def __init__(self, *a, **k):
        pass

    def transcribe(self, path, language=None, **k):
        return iter(self.segments), types.SimpleNamespace(language=language or "zh", language_probability=0.97)


class TranscribeMainTest(unittest.TestCase):
    def run_main(self, *extra, segments=()):
        d = tmp()
        media = d / "clip.wav"
        media.write_bytes(b"RIFF")
        out = d / "tr.json"
        model = type("Model", (FakeModel,), {"segments": list(segments)})
        fake = types.SimpleNamespace(WhisperModel=model)
        buf = io.StringIO()
        with mock.patch.dict(sys.modules, {"faster_whisper": fake}), \
                mock.patch.object(transcribe, "_register_cuda_dlls", return_value=None), \
                contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            code = transcribe.main([str(media), "--output", str(out), "--device", "cpu", *extra])
        return code, json.loads(buf.getvalue()), out

    def test_no_speech_writes_nothing(self):
        code, reply, out = self.run_main(segments=[])
        self.assertEqual((code, reply["code"]), (1, "NO_SPEECH"))
        self.assertFalse(out.exists())

    def test_chinese_maps_to_cmn_hans(self):
        code, reply, out = self.run_main(segments=[segment(("\u4f60\u597d", 0.0, 0.5, 0.9))])
        self.assertEqual((code, reply["language"], reply["words"]), (0, "cmn-hans", 1))
        saved = json.loads(out.read_bytes().decode("utf-8"))
        self.assertEqual(transcript.validate(saved), [])
        self.assertNotIn(b"\r", out.read_bytes())

    def test_adobe_language_override(self):
        code, reply, _ = self.run_main("--adobe-language", "EN-GB", segments=[segment(("Hi.", 0.0, 0.5, 0.9))])
        self.assertEqual((code, reply["language"]), (0, "en-gb"))
        code, reply, _ = self.run_main("--adobe-language", "en-cn", segments=[segment(("Hi.", 0.0, 0.5, 0.9))])
        self.assertEqual((code, reply["code"]), (1, "BAD_PARAMS"))

    def test_missing_media(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = transcribe.main([str(tmp() / "none.wav"), "--output", str(tmp() / "x.json")])
        self.assertEqual((code, json.loads(buf.getvalue())["code"]), (1, "FILE_NOT_FOUND"))


class DigestTest(unittest.TestCase):
    def test_punctuation_attaches_and_ends_the_sentence(self):
        d = doc()
        d["segments"][0]["words"] += [word("Again", 1.0), word("!", 1.3, eos=True, kind="punctuation")]
        sentences = digest.digest(d)["sentences"]
        self.assertEqual([s["text"] for s in sentences], ["Hello world.", "Again!"])
        self.assertEqual(sentences[0]["end"], 0.6)  # the last word's end, not the punctuation's
        self.assertEqual(sentences[0]["avgConfidence"], 0.9)

    def test_chinese_joins_without_spaces(self):
        d = doc(language="cmn-hans")
        d["segments"][0]["words"] = [word("\u4f60\u597d", 0.0), word("\u4e16\u754c", 0.3, eos=True)]
        self.assertEqual(digest.digest(d)["sentences"][0]["text"], "\u4f60\u597d\u4e16\u754c")

    def test_main_reports_unreadable_files_with_a_code(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = digest.main([str(tmp() / "none.json")])
        reply = json.loads(buf.getvalue())
        self.assertEqual((code, reply["code"]), (1, "FILE_NOT_FOUND"))
        self.assertTrue(reply["error"].startswith("Cannot read transcript"))


if __name__ == "__main__":
    unittest.main()
