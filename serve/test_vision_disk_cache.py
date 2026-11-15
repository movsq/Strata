import io
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from serve.server import Vision


class PersistentVision(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='vision-disk-test-'))
        self.cfg = {'cache_dir': str(self.root / 'cache'), 'max_tokens': 2048}
        for name in ['exe', 'model', 'mmproj']:
            path = self.root / name
            path.write_bytes(b'file')
            self.cfg[name] = str(path)

    def sve(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(struct.pack('<4s4I', b'SVE1', 2, 2, 1, 3) + b'\0' * 24)

    def test_same_settings_reuse_images_after_shutdown(self):
        first = Vision(self.cfg, lazy=True)
        source = b'normalized-image'
        import hashlib
        key = hashlib.sha256(source).hexdigest()[:32]
        self.sve(first.dir / f'{key}.sve')
        first.shutdown()
        second = Vision(self.cfg, lazy=True)
        with mock.patch.object(Vision, 'normalize', return_value=source):
            path, tokens = second.encode(source)
            self.assertEqual(tokens, 2)
            self.assertTrue(path.is_file())
            self.assertTrue(second.all_cached([source]))
            self.assertIsNone(second.proc)

    def test_identity_changes_for_model_projector_encoder_and_settings(self):
        original = Vision.cache_directory(self.cfg)
        self.assertEqual(original, Vision.cache_directory(self.cfg))
        for name in ['exe', 'model', 'mmproj']:
            path = Path(self.cfg[name])
            path.write_bytes(path.read_bytes() + b'changed')
            self.assertNotEqual(original, Vision.cache_directory(self.cfg))
            original = Vision.cache_directory(self.cfg)
        self.assertNotEqual(original, Vision.cache_directory({**self.cfg, 'max_tokens': 1024}))

    def test_partial_and_invalid_encodings_are_ignored(self):
        directory = Vision.cache_directory(self.cfg)
        directory.mkdir(parents=True)
        self.sve(directory / ('a' * 32 + '.sve'))
        (directory / ('b' * 32 + '.sve')).write_bytes(b'SVE1')
        self.sve(directory / 'wrong-name.sve')
        self.assertEqual(list(Vision.read_cache(directory)), ['a' * 32])

    def test_disabled_cache_uses_the_existing_temporary_directory(self):
        self.assertIsNone(Vision.cache_directory({k: v for k, v in self.cfg.items() if k != 'cache_dir'}))


if __name__ == '__main__':
    unittest.main()
