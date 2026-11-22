"""Tests for the parts of 橙芯绘话 that are easy to break and hard to notice.

Run with:  python -m unittest discover -s tests -v

These are deliberately dependency-free (unittest only) and do not start any
model: they cover the pure logic — config merging, the GPU mutual-exclusion
state machine, chat merge semantics, image cleanup rules and the downloader's
resume/verify behaviour.
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)


# --------------------------------------------------------------------------
# config: defaults, overrides, env priority
# --------------------------------------------------------------------------
class ConfigTests(unittest.TestCase):
    def setUp(self):
        import importlib
        import config
        self.config = importlib.reload(config)

    def test_defaults_present(self):
        cfg = self.config._merge(self.config.DEFAULTS, {})
        self.assertEqual(cfg["host"], "127.0.0.1")
        self.assertIn("models", cfg)
        self.assertGreaterEqual(len(cfg["models"]), 1)

    def test_partial_override_keeps_siblings(self):
        base = {"a": 1, "nested": {"x": 1, "y": 2}}
        got = self.config._merge(base, {"nested": {"y": 9}})
        self.assertEqual(got["nested"]["x"], 1)   # untouched key survives
        self.assertEqual(got["nested"]["y"], 9)   # override applies
        self.assertEqual(got["a"], 1)

    def test_explicit_none_wins(self):
        got = self.config._merge({"k": "v"}, {"k": None})
        self.assertIsNone(got["k"])

    def test_env_port_override(self):
        old = os.environ.get("CHENGXIN_PORT")
        os.environ["CHENGXIN_PORT"] = "9999"
        try:
            env = self.config._env_overrides()
            self.assertEqual(env["port"], 9999)
        finally:
            if old is None:
                os.environ.pop("CHENGXIN_PORT", None)
            else:
                os.environ["CHENGXIN_PORT"] = old

    def test_env_llama_dir(self):
        old = os.environ.get("CHENGXIN_LLAMA_DIR")
        os.environ["CHENGXIN_LLAMA_DIR"] = r"D:\models"
        try:
            self.assertEqual(self.config._env_overrides()["llama_dir"], r"D:\models")
        finally:
            if old is None:
                os.environ.pop("CHENGXIN_LLAMA_DIR", None)
            else:
                os.environ["CHENGXIN_LLAMA_DIR"] = old

    def test_priority_env_beats_file(self):
        """config.json < 环境变量: env must win in the final merge order."""
        merged = self.config._merge(self.config.DEFAULTS, {"port": 1234})
        merged = self.config._merge(merged, {"port": 5678})
        self.assertEqual(merged["port"], 5678)


# --------------------------------------------------------------------------
# download: resume, size check, GGUF header check
# --------------------------------------------------------------------------
class DownloadTests(unittest.TestCase):
    def setUp(self):
        import download
        self.dl = download
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_human_readable(self):
        self.assertEqual(self.dl.human(512), "512.0 B")
        self.assertIn("KB", self.dl.human(2048))
        self.assertIn("GB", self.dl.human(2 * 1024 ** 3))

    def test_resolve_direct_url(self):
        url = self.dl.resolve_url({"url": "https://x/y.gguf"})
        self.assertEqual(url, "https://x/y.gguf")

    def test_resolve_hf_pair(self):
        url = self.dl.resolve_url(
            {"hf_repo": "org/repo", "hf_file": "m.gguf"}, "https://hf-mirror.com")
        self.assertEqual(url, "https://hf-mirror.com/org/repo/resolve/main/m.gguf")

    def test_resolve_empty(self):
        self.assertEqual(self.dl.resolve_url({}), "")
        self.assertEqual(self.dl.resolve_url({"hf_repo": "a/b"}), "")

    def test_verify_missing_file(self):
        self.assertFalse(self.dl.verify(os.path.join(self.tmp, "nope.gguf"))["ok"])

    def test_verify_empty_file(self):
        p = os.path.join(self.tmp, "empty.gguf")
        with open(p, "wb"):
            pass
        self.assertFalse(self.dl.verify(p)["ok"])

    def test_verify_good_gguf(self):
        p = os.path.join(self.tmp, "ok.gguf")
        with open(p, "wb") as fh:
            fh.write(b"GGUF" + b"\x00" * 60)
        self.assertTrue(self.dl.verify(p)["ok"])

    def test_verify_bad_gguf_magic(self):
        p = os.path.join(self.tmp, "bad.gguf")
        with open(p, "wb") as fh:
            fh.write(b"HTML" + b"\x00" * 60)
        res = self.dl.verify(p)
        self.assertFalse(res["ok"])
        self.assertIn("GGUF", res["message"])

    def test_verify_size_mismatch(self):
        p = os.path.join(self.tmp, "short.gguf")
        with open(p, "wb") as fh:
            fh.write(b"GGUF" + b"\x00" * 60)
        res = self.dl.verify(p, expect_bytes=9999)
        self.assertFalse(res["ok"])
        self.assertIn("大小不符", res["message"])

    def test_verify_json(self):
        good = os.path.join(self.tmp, "a.json")
        with open(good, "w", encoding="utf-8") as fh:
            json.dump({"x": 1}, fh)
        self.assertTrue(self.dl.verify(good)["ok"])
        bad = os.path.join(self.tmp, "b.json")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertFalse(self.dl.verify(bad)["ok"])

    def test_missing_lists_only_absent_files(self):
        present = os.path.join(self.tmp, "here.gguf")
        with open(present, "wb") as fh:
            fh.write(b"GGUF")
        absent = os.path.join(self.tmp, "gone.gguf")
        out = self.dl.missing({"model": present, "mmproj": absent})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][1], absent)

    def test_missing_ignores_empty_fields(self):
        self.assertEqual(self.dl.missing({"model": "", "mmproj": ""}), [])


# --------------------------------------------------------------------------
# hub: the mutual-exclusion state machine and config validation
# --------------------------------------------------------------------------
class HubTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import hub
        cls.hub = hub

    def test_models_are_built_from_config(self):
        self.assertGreaterEqual(len(self.hub.MODELS), 1)
        for mid, cfg in self.hub.MODELS.items():
            self.assertEqual(cfg["id"], mid)
            self.assertIsInstance(cfg["port"], int)
            self.assertTrue(0 < cfg["port"] < 65536)

    def test_default_model_exists(self):
        self.assertIn(self.hub.DEFAULT_MODEL, self.hub.MODELS)

    def test_no_duplicate_ports_in_default_config(self):
        """Two services on one port is the classic typo; catch it early."""
        ports = [c["port"] for c in self.hub.MODELS.values()]
        if self.hub.SD.get("enabled"):
            ports.append(self.hub.SD["port"])
        if self.hub.TTS.get("enabled"):
            ports.append(self.hub.TTS["port"])
        self.assertEqual(len(ports), len(set(ports)),
                         f"端口冲突: {ports}")

    def test_config_problems_shape(self):
        probs = self.hub.config_problems()
        self.assertIsInstance(probs, list)
        for p in probs:
            self.assertIsInstance(p, str)

    def test_config_problems_detects_bad_path(self):
        """A model pointing at a missing bat must be reported, not ignored."""
        orig = dict(self.hub.MODELS)
        try:
            self.hub.MODELS["__test__"] = {
                "id": "__test__", "label": "T", "port": 9999,
                "bat": r"C:\definitely\not\here.bat", "model": "", "ctx": 1,
            }
            probs = self.hub.config_problems()
            self.assertTrue(any("不存在" in p for p in probs), probs)
        finally:
            self.hub.MODELS.clear()
            self.hub.MODELS.update(orig)

    def test_wait_port_free_returns_quickly_for_free_port(self):
        import time
        t0 = time.time()
        ok = self.hub.wait_port_free(59999, timeout=3)
        self.assertTrue(ok)
        self.assertLess(time.time() - t0, 3)

    def test_gpu_memory_shape(self):
        mem = self.hub.gpu_memory()
        if mem is None:
            self.skipTest("本机没有 nvidia-smi")
        self.assertIn("total", mem)
        self.assertIn("free", mem)
        self.assertGreater(mem["total"], 0)
        self.assertLessEqual(mem["free"], mem["total"])

    def test_probe_reports_missing_files(self):
        cfg = {**next(iter(self.hub.MODELS.values())),
               "model": r"C:\nope\x.gguf", "mmproj": ""}
        cfg = dict(cfg)
        state = self.hub.probe(cfg)
        # probe must not raise even when the model file is absent
        self.assertIn("missing", state)
        self.assertIn("id", state)


# --------------------------------------------------------------------------
# chats: merge by id, tombstones, image extraction
# --------------------------------------------------------------------------
class ChatStoreTests(unittest.TestCase):
    """Exercise the merge rules against a throwaway chats.json."""

    def setUp(self):
        import hub
        self.hub = hub
        self.tmp = tempfile.mkdtemp()
        self.orig_file = hub.CHATS_FILE
        self.orig_img = hub.CHAT_IMAGES
        hub.CHATS_FILE = os.path.join(self.tmp, "chats.json")
        hub.CHAT_IMAGES = os.path.join(self.tmp, "images")
        os.makedirs(hub.CHAT_IMAGES, exist_ok=True)
        with open(hub.CHATS_FILE, "w", encoding="utf-8") as fh:
            json.dump({"current": None, "chats": []}, fh)

    def tearDown(self):
        import shutil
        self.hub.CHATS_FILE = self.orig_file
        self.hub.CHAT_IMAGES = self.orig_img
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_merge_adds_new_chat(self):
        res = self.hub.merge_chats({"chats": [
            {"id": "a", "title": "A", "messages": [], "updatedAt": 1}]})
        self.assertTrue(res.get("ok", True))
        stored = self.hub._read_chats_unlocked()
        self.assertEqual([c["id"] for c in stored["chats"]], ["a"])

    def test_merge_keeps_both(self):
        self.hub.merge_chats({"chats": [
            {"id": "a", "title": "A", "messages": [], "updatedAt": 1}]})
        self.hub.merge_chats({"chats": [
            {"id": "b", "title": "B", "messages": [], "updatedAt": 2}]})
        ids = sorted(c["id"] for c in self.hub._read_chats_unlocked()["chats"])
        self.assertEqual(ids, ["a", "b"])

    def test_merge_updates_existing(self):
        self.hub.merge_chats({"chats": [
            {"id": "a", "title": "old", "messages": [], "updatedAt": 1}]})
        self.hub.merge_chats({"chats": [
            {"id": "a", "title": "new", "messages": [], "updatedAt": 5}]})
        stored = self.hub._read_chats_unlocked()
        self.assertEqual(stored["chats"][0]["title"], "new")

    def test_tombstone_removes_chat(self):
        self.hub.merge_chats({"chats": [
            {"id": "a", "title": "A", "messages": [], "updatedAt": 1}]})
        self.hub.merge_chats({"chats": [], "deleted": ["a"]})
        self.assertEqual(self.hub._read_chats_unlocked()["chats"], [])

    def test_image_extracted_to_file(self):
        """A data URL must become a file reference, not stay inline."""
        payload = ("data:image/png;base64,"
                   "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
                   "nGP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==")
        self.hub.merge_chats({"chats": [{
            "id": "a", "title": "A", "updatedAt": 1,
            "messages": [{"role": "assistant", "content": "",
                          "images": [{"dataUrl": payload, "name": "x.png"}]}],
        }]})
        with open(self.hub.CHATS_FILE, encoding="utf-8") as fh:
            raw = json.load(fh)
        img = raw["chats"][0]["messages"][0]["images"][0]
        self.assertEqual(img.get("dataUrl"), "")
        self.assertTrue(img.get("file"))
        self.assertTrue(os.path.exists(os.path.join(self.hub.CHAT_IMAGES, img["file"])))

    def test_image_rehydrated_on_read(self):
        payload = ("data:image/png;base64,"
                   "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
                   "nGP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==")
        self.hub.merge_chats({"chats": [{
            "id": "a", "title": "A", "updatedAt": 1,
            "messages": [{"role": "assistant", "content": "",
                          "images": [{"dataUrl": payload, "name": "x.png"}]}],
        }]})
        data = self.hub.read_chats()
        img = data["chats"][0]["messages"][0]["images"][0]
        self.assertTrue(img["dataUrl"].startswith("data:image/"))


# --------------------------------------------------------------------------
# image cleanup: only unreferenced + old files go
# --------------------------------------------------------------------------
class CleanupTests(unittest.TestCase):
    def setUp(self):
        import hub
        self.hub = hub
        self.tmp = tempfile.mkdtemp()
        self.orig_file = hub.CHATS_FILE
        self.orig_img = hub.CHAT_IMAGES
        hub.CHATS_FILE = os.path.join(self.tmp, "chats.json")
        hub.CHAT_IMAGES = os.path.join(self.tmp, "images")
        os.makedirs(hub.CHAT_IMAGES, exist_ok=True)
        with open(hub.CHATS_FILE, "w", encoding="utf-8") as fh:
            json.dump({"current": None, "chats": []}, fh)

    def tearDown(self):
        import shutil
        self.hub.CHATS_FILE = self.orig_file
        self.hub.CHAT_IMAGES = self.orig_img
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _touch(self, name: str, age_days: float):
        p = os.path.join(self.hub.CHAT_IMAGES, name)
        with open(p, "wb") as fh:
            fh.write(b"x" * 10)
        old = __import__("time").time() - age_days * 86400
        os.utime(p, (old, old))
        return p

    def test_old_unreferenced_is_deleted(self):
        self._touch("old.png", 40)
        res = self.hub.images_cleanup(30)
        self.assertEqual(res["removed"], 1)
        self.assertFalse(os.path.exists(os.path.join(self.hub.CHAT_IMAGES, "old.png")))

    def test_recent_unreferenced_is_kept(self):
        self._touch("new.png", 1)
        res = self.hub.images_cleanup(30)
        self.assertEqual(res["removed"], 0)

    def test_old_but_referenced_is_kept(self):
        """A picture a chat still uses must survive however old it is."""
        p = self._touch("used.png", 400)
        with open(self.hub.CHATS_FILE, "w", encoding="utf-8") as fh:
            json.dump({"current": None, "chats": [{
                "id": "a", "title": "A", "updatedAt": 1,
                "messages": [{"role": "assistant", "content": "",
                              "images": [{"dataUrl": "", "file": "used.png"}]}],
            }]}, fh)
        res = self.hub.images_cleanup(30)
        self.assertEqual(res["removed"], 0)
        self.assertTrue(os.path.exists(p))


if __name__ == "__main__":
    unittest.main(verbosity=2)
