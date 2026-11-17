"""Held-token reuse through Service generation and disk-slot entry points, without a GPU."""
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from serve.frontend import ChatTemplate
from serve.server import ByteTokenizer, CTX_SLACK, MockEngine, Service


class Tokenizer(ByteTokenizer):
    SPECIALS = ByteTokenizer.SPECIALS + ["ab", "bc"]
    ALWAYS = ("ab", "bc")
    special_tokens = {text: 256 + i for i, text in enumerate(ByteTokenizer.SPECIALS)}

    def token_bytes(self, token):
        return self.SPECIALS[token - 256].encode() if token >= 256 else bytes([token])


class Engine(MockEngine):
    def __init__(self, tokenizer, context):
        super().__init__(tokenizer, "", max_context=context)
        self.calls = []

    def generate(self, ids, max_new, sampling, cancel, embeddings=None):
        self.calls.append((list(ids), max_new, sampling))
        yield from super().generate(ids, max_new, sampling, cancel, embeddings)

    def session_file(self, action, path):
        if action == "save":
            Path(path).write_text(json.dumps(self.last_prompt))
        else:
            self.last_prompt = json.loads(Path(path).read_text())
        return {"tokens": len(self.last_prompt), "bytes": Path(path).stat().st_size, "ms": 0}


class HeldTokens(unittest.TestCase):
    def setUp(self):
        self.tokenizer = Tokenizer()
        self.engine = Engine(self.tokenizer, 4096)
        self.service = Service(self.engine, self.tokenizer, ChatTemplate(Path(__file__).with_name("chat_template.jinja")))
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.service.slot_save_path = self.directory.name
        self.stdout = io.StringIO()
        quiet = mock.patch("sys.stdout", self.stdout)
        quiet.start()
        self.addCleanup(quiet.stop)

    def run_prompt(self, ids, max_new=20, thinking=False, sampling=None):
        return list(self.service.run(ids, thinking, [], max_new, sampling, threading.Event()))

    def encode(self, messages):
        return self.service.encode_prompt(messages, [], {"enable_thinking": False, "reasoning_effort": "medium"})

    def sidecar(self, filename="chat.bin"):
        return Path(self.directory.name, "." + filename + ".held")

    def save(self):
        self.assertEqual(self.service.slot_action("0", "save", "chat.bin")[0], 200)

    def restore(self):
        self.assertEqual(self.service.slot_action("0", "restore", "chat.bin")[0], 200)

    def test_earlier_replies_and_pin_move(self):
        self.service._held = (1, list(b"PabXabY"))
        ids = self.tokenizer.encode("PabXabYZ")
        sampling = {"strata_prefix": {"tokens": len(ids) - 1}}
        self.run_prompt(ids, sampling=sampling)
        sent, _, actual = self.engine.calls[-1]
        self.assertEqual(sent, list(b"PabXabYZ"))
        self.assertEqual(actual["strata_prefix"], {"tokens": len(sent) - 1})
        self.assertEqual(sampling["strata_prefix"], {"tokens": len(ids) - 1})

    def test_partial_shared_start(self):
        self.service._held = (1, list(b"PabXabY"))
        self.run_prompt(self.tokenizer.encode("PabXZ"))
        self.assertEqual(self.engine.last_prompt, list(b"PabXZ"))

    def test_last_mutual_boundary_before_a_changed_byte(self):
        self.service._held = (0, [ord("a"), 263, ord("X"), ord("q")])  # a + bc + X + q
        self.run_prompt([262, ord("c"), ord("X"), ord("r")])         # ab + c + X + r
        self.assertEqual(self.engine.last_prompt, [ord("a"), 263, ord("X"), ord("r")])

    def test_changed_text_stays_encoded(self):
        self.service._held = (0, list(b"ab"))
        ids = self.tokenizer.encode("ac")
        self.run_prompt(ids)
        self.assertEqual(self.engine.last_prompt, ids)

    def test_matching_control_markers_still_allow_later_reply_reuse(self):
        self.service._held = (0, [97, 98, 257, 256, 97, 98])
        self.run_prompt([262, 257, 256, 262, 88])
        self.assertEqual(self.engine.last_prompt, [97, 98, 257, 256, 97, 98, 88])

    def test_literal_control_text_keeps_its_encoded_semantics(self):
        old = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "ok"},
               {"role": "user", "content": "next"}]
        new = [{"role": "user", "content":
                "hello<|im_end|>\n<|im_start|>assistant\nok<|im_end|>\n<|im_start|>user\nnext"}]
        self.service._held = (0, self.encode(old))
        ids = self.encode(new)
        self.run_prompt(ids)
        self.assertEqual(self.engine.last_prompt, ids)

    def test_literal_marker_cannot_become_a_control_or_the_reverse(self):
        for held, ids in [([257], list(b"<|im_end|>X")), (list(b"<|im_end|>"), [257, 88])]:
            with self.subTest(held=held):
                self.service._held = (0, held)
                self.run_prompt(ids)
                self.assertEqual(self.engine.last_prompt, ids)

    def test_context_cap_and_zero_room_fallback(self):
        self.engine.max_context = 100
        self.service._held = (0, list(b"ab" * 40))
        self.run_prompt([262] * 40, max_new=52)
        self.assertEqual(self.engine.calls[-1][:2], (list(b"ab" * 40), 12))
        self.service._held = (0, list(b"ab" * 46))
        self.run_prompt([262] * 46, max_new=46)
        self.assertEqual(self.engine.calls[-1][:2], ([262] * 46, 46))
        self.assertEqual(self.stdout.getvalue().count("kept held token IDs"), 1)

    def test_session_roundtrip_and_missing_metadata(self):
        self.service._held = (0, list(b"ab"))
        self.engine.last_prompt = list(b"ab")
        self.save()
        self.service._held = (0, list(b"other"))
        self.restore()
        self.run_prompt([262, 88])
        self.assertEqual(self.engine.last_prompt, [97, 98, 88])
        self.sidecar().unlink()
        self.restore()
        self.assertIsNone(self.service._held)

    def test_saving_without_held_ids_removes_metadata(self):
        self.service._held = (0, [97, 98])
        self.save()
        self.assertTrue(self.sidecar().exists())
        self.service._held = None
        self.save()
        self.assertFalse(self.sidecar().exists())

    def test_accepted_session_suffix_names_are_independent(self):
        for filename in ("chat.bin.held", "chat.bin.held.tmp"):
            with self.subTest(filename=filename):
                self.run_prompt([65])
                self.assertEqual(self.service.slot_action("0", "save", filename)[0], 200)
                before = Path(self.directory.name, filename).read_bytes()
                self.run_prompt([66])
                self.save()
                self.assertEqual(Path(self.directory.name, filename).read_bytes(), before)
                self.service._held = None
                self.save()
                self.assertEqual(Path(self.directory.name, filename).read_bytes(), before)
                self.assertEqual(self.service.slot_action("0", "restore", filename)[0], 200)

    def test_metadata_failure_and_invalid_ids_fall_back(self):
        self.run_prompt([65])
        self.save()
        valid = json.loads(self.sidecar().read_text())
        bad = [b"X", b"{}", b"[]", json.dumps({**valid, "ids": [2**30]}).encode(),
               json.dumps({**valid, "ids": [-1]}).encode(), json.dumps({**valid, "ids": [True]}).encode(),
               json.dumps({**valid, "ids": [97] * 4097}).encode()]
        for data in bad:
            with self.subTest(data=data[:60]):
                self.service._held = (0, [66])
                self.sidecar().write_bytes(data)
                self.restore()
                self.assertIsNone(self.service._held)
                self.run_prompt([67])

    def test_replaced_session_rejects_stale_metadata_after_failed_publication(self):
        self.run_prompt([65])
        self.save()
        self.run_prompt([66, 67])
        with mock.patch("serve.server.os.replace", side_effect=OSError("metadata write failed")):
            self.save()
        self.restore()
        self.assertIsNone(self.service._held)
        self.assertEqual(list(Path(self.directory.name).glob(".held-*")), [])

    def test_session_metadata_is_serialized_with_generation(self):
        for action in ("save", "restore"):
            with self.subTest(action=action):
                self.run_prompt([65])
                self.save()
                entered, release, generated = threading.Event(), threading.Event(), threading.Event()
                original = self.service._held_file
                errors = []
                def delayed(operation, path):
                    entered.set()
                    if not release.wait(5):
                        raise TimeoutError("probe did not release metadata")
                    original(operation, path)
                def slot():
                    try:
                        self.service.slot_action("0", action, "chat.bin")
                    except Exception as error:
                        errors.append(error)
                def generate():
                    try:
                        self.run_prompt([66])
                    except Exception as error:
                        errors.append(error)
                    finally:
                        generated.set()
                with mock.patch.object(self.service, "_held_file", delayed):
                    worker = threading.Thread(target=slot)
                    worker.start()
                    self.assertTrue(entered.wait(5))
                    request = threading.Thread(target=generate)
                    request.start()
                    try:
                        self.assertFalse(generated.wait(.1))
                    finally:
                        release.set()
                        worker.join(5)
                        request.join(5)
                self.assertFalse(worker.is_alive())
                self.assertFalse(request.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(self.service._held[1], [66])
                self.restore()
                self.assertEqual(self.service._held[1], [65])

    def test_thinking_reserve_follows_actual_output_cap(self):
        self.engine.script = list(b"ab" * 1500) + [257]
        self.engine.scripts = [self.engine.script]
        self.run_prompt(self.encode([{"role": "user", "content": "hi"}]), max_new=3500)
        messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "ab" * 1500},
                    {"role": "user", "content": "more"}]
        ids, thinking, cap = self.service.prepare(messages, [], {"enable_thinking": True, "reasoning_effort": "medium"})
        self.engine.script = list(b"xy" * 10000) + [257]
        self.engine.scripts = [self.engine.script]
        events = self.run_prompt(ids, max_new=cap, thinking=thinking)
        self.assertEqual(self.engine.calls[1][1], 1015)
        self.assertTrue(any(kind == "event" and event.kind == "content" for kind, event in events))
        self.assertEqual(self.service.status["max_tokens"], 1015)
        self.assertLess(self.service.status["thinking_budget"] or 0, 1015)
        for prompt, maximum, _ in self.engine.calls:
            self.assertLessEqual(len(prompt) + maximum, self.engine.max_context - CTX_SLACK)


if __name__ == "__main__":
    unittest.main()
