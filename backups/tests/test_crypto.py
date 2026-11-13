"""AES-256-GCM file encryption: round trips and every kind of tampering."""

import os
import struct
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from backups import crypto
from backups.crypto import DecryptionError

KEY = bytes(range(32))
OTHER = bytes(range(1, 33))
CHUNK = 4096  # the smallest chunk the reader accepts, so multi-chunk files stay small
HEADER = crypto.HEADER.size
SEALED = CHUNK + crypto.TAG


class CryptoBase(SimpleTestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="coven-crypto-"))
        self.addCleanup(__import__("shutil").rmtree, self.dir, True)

    def make(self, size, key=KEY, name="a"):
        plain = self.dir / f"{name}.bin"
        plain.write_bytes(os.urandom(size))
        enc = self.dir / f"{name}.enc"
        crypto.encrypt_file(plain, enc, key, chunk_size=CHUNK)
        return plain, enc

    def decrypt(self, enc, key=KEY, name="out"):
        out = self.dir / f"{name}.bin"
        crypto.decrypt_file(enc, out, key)
        return out


class RoundTripTests(CryptoBase):
    def test_round_trips_at_every_size_boundary(self):
        for size in (0, 1, 100, CHUNK - 1, CHUNK, CHUNK + 1, 2 * CHUNK, 2 * CHUNK + 7, 5 * CHUNK):
            plain, enc = self.make(size, name=f"s{size}")
            out = self.decrypt(enc, name=f"o{size}")
            self.assertEqual(out.read_bytes(), plain.read_bytes(), size)

    def test_hashes_and_sizes_are_reported_correctly(self):
        plain = self.dir / "p.bin"
        plain.write_bytes(os.urandom(3 * CHUNK + 5))
        enc = self.dir / "p.enc"
        sha, size, plain_sha = crypto.encrypt_file(plain, enc, KEY, chunk_size=CHUNK)
        self.assertEqual(sha, crypto.sha256_file(enc))
        self.assertEqual(size, enc.stat().st_size)
        self.assertEqual(plain_sha, crypto.sha256_file(plain))
        self.assertEqual(crypto.decrypt_file(enc, None, KEY), plain_sha)  # verify-only mode

    def test_the_ciphertext_hides_the_plaintext_and_is_never_the_same_twice(self):
        plain = self.dir / "p.bin"
        plain.write_bytes(b"SECRET-MARKER-" * 500)
        a, b = self.dir / "a.enc", self.dir / "b.enc"
        crypto.encrypt_file(plain, a, KEY, chunk_size=CHUNK)
        crypto.encrypt_file(plain, b, KEY, chunk_size=CHUNK)
        self.assertNotIn(b"SECRET-MARKER", a.read_bytes())
        self.assertNotEqual(a.read_bytes(), b.read_bytes())  # a fresh random nonce each time

    def test_the_header_carries_only_a_non_secret_fingerprint(self):
        _, enc = self.make(10)
        version, chunk, fp, base = crypto.read_header(enc)
        self.assertEqual((version, chunk, fp), (1, CHUNK, crypto.fingerprint(KEY)))
        self.assertNotIn(KEY, enc.read_bytes())

    def test_key_validation(self):
        for bad in (b"", b"short", bytes(31), bytes(33), "x" * 32, None):
            with self.assertRaises(ValueError):
                crypto.encrypt_file(self.dir / "x", self.dir / "y", bad)
        self.assertNotEqual(crypto.fingerprint(KEY), crypto.fingerprint(OTHER))
        self.assertNotIn(KEY.hex(), crypto.fingerprint(KEY))


class WrongKeyAndTamperTests(CryptoBase):
    def assertRejected(self, enc, key=KEY):
        out = self.dir / "rejected.bin"
        with self.assertRaises(DecryptionError):
            crypto.decrypt_file(enc, out, key)
        self.assertFalse(out.exists(), "a failed decryption must not leave a partial file")

    def test_the_wrong_key_is_rejected_with_a_clear_message(self):
        _, enc = self.make(3 * CHUNK)
        with self.assertRaisesRegex(DecryptionError, "different key"):
            crypto.decrypt_file(enc, self.dir / "o", OTHER)

    def test_any_single_flipped_byte_is_detected_in_every_region(self):
        _, enc = self.make(3 * CHUNK + 10)
        data = enc.read_bytes()
        positions = [0, 7, 8, 9, 13, 14, 21, 22, 28, HEADER - 1, HEADER, HEADER + 100]
        positions += [HEADER + SEALED - 1, HEADER + SEALED, HEADER + 2 * SEALED + 5, len(data) - 1]
        for pos in positions:
            for mask in (0x01, 0x80):
                tampered = bytearray(data)
                tampered[pos] ^= mask
                bad = self.dir / f"t{pos}-{mask}.enc"
                bad.write_bytes(bytes(tampered))
                self.assertRejected(bad)

    def test_truncation_is_detected_at_a_chunk_boundary_and_inside_a_chunk(self):
        _, enc = self.make(3 * CHUNK + 10)
        data = enc.read_bytes()
        for cut in (
            HEADER + SEALED,
            HEADER + 2 * SEALED,
            len(data) - 1,
            len(data) - 17,
            HEADER + 5,
            HEADER,
        ):
            bad = self.dir / f"cut{cut}.enc"
            bad.write_bytes(data[:cut])
            self.assertRejected(bad)

    def test_appended_data_and_a_second_copy_are_detected(self):
        _, enc = self.make(2 * CHUNK + 3)
        data = enc.read_bytes()
        for extra in (b"\x00", os.urandom(40), data[HEADER:]):
            bad = self.dir / "ext.enc"
            bad.write_bytes(data + extra)
            self.assertRejected(bad)

    def test_dropping_duplicating_or_swapping_chunks_is_detected(self):
        _, enc = self.make(4 * CHUNK + 1)
        data = enc.read_bytes()
        chunks = [data[HEADER + i * SEALED : HEADER + (i + 1) * SEALED] for i in range(4)] + [
            data[HEADER + 4 * SEALED :]
        ]
        header = data[:HEADER]
        variants = {
            "drop": [0, 2, 3, 4],
            "dup": [0, 1, 1, 2, 3, 4],
            "swap": [1, 0, 2, 3, 4],
            "reverse": [4, 3, 2, 1, 0],
        }
        for name, order in variants.items():
            bad = self.dir / f"{name}.enc"
            bad.write_bytes(header + b"".join(chunks[i] for i in order))
            self.assertRejected(bad)

    def test_a_chunk_from_another_backup_is_detected(self):
        _, a = self.make(2 * CHUNK, name="a")
        _, b = self.make(2 * CHUNK, name="b")
        da, db = a.read_bytes(), b.read_bytes()
        spliced = da[: HEADER + SEALED] + db[HEADER + SEALED :]
        bad = self.dir / "spliced.enc"
        bad.write_bytes(spliced)
        self.assertRejected(bad)

    def test_not_a_backup_file_and_degenerate_files(self):
        for name, content in (
            ("empty", b""),
            ("short", b"COVENBK1"),
            ("junk", os.urandom(500)),
            ("text", b"hello" * 100),
        ):
            bad = self.dir / name
            bad.write_bytes(content)
            self.assertRejected(bad)

    def test_header_with_a_wrong_version_or_chunk_size_is_refused(self):
        _, enc = self.make(100)
        data = bytearray(enc.read_bytes())
        data[8] = 2  # version
        bad = self.dir / "v2.enc"
        bad.write_bytes(bytes(data))
        self.assertRejected(bad)
        data = bytearray(enc.read_bytes())
        data[9:13] = struct.pack(">I", 1)  # absurd chunk size
        bad.write_bytes(bytes(data))
        self.assertRejected(bad)

    def test_verify_only_mode_also_detects_tampering(self):
        _, enc = self.make(2 * CHUNK)
        data = bytearray(enc.read_bytes())
        data[HEADER + 3] ^= 1
        bad = self.dir / "v.enc"
        bad.write_bytes(bytes(data))
        with self.assertRaises(DecryptionError):
            crypto.decrypt_file(bad, None, KEY)
