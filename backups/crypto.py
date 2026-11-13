"""Streaming AES-256-GCM for backup files.

File format (all integers big-endian)::

    magic "COVENBK1" (8) | version u8 | chunk_size u32 | key fingerprint (8) | base nonce (8)
    then chunks, each = AES-GCM(plaintext chunk of ``chunk_size`` bytes, the last one shorter)

* nonce  = base nonce (8) + chunk counter u32;
* AAD    = the header + counter + a FINAL flag, so a reordered, dropped, duplicated, truncated or
  extended file fails authentication, as does any change to the header;
* the final chunk is the one followed by end of file (an empty source still has one empty chunk).

Nothing here reads or stores a key anywhere: callers pass the 32-byte key in.
"""

import hashlib
import hmac
import os
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"COVENBK1"
VERSION = 1
CHUNK_SIZE = 1024 * 1024
TAG = 16
HEADER = struct.Struct(">8sBI8s8s")  # magic, version, chunk size, fingerprint, base nonce
KEY_BYTES = 32


class DecryptionError(Exception):
    """Wrong key, damaged or tampered file, or not a backup file."""


def fingerprint(key):
    """A non-secret identifier of a key (safe to store in manifests and logs)."""
    return hashlib.sha256(b"covens-library-backup-key-v1" + key).hexdigest()[:16]


def _check_key(key):
    if not isinstance(key, (bytes, bytearray)) or len(key) != KEY_BYTES:
        raise ValueError("the backup key must be exactly 32 bytes")


def _nonce(base, counter):
    return base + struct.pack(">I", counter)


def _aad(header, counter, final):
    return header + struct.pack(">I", counter) + (b"\x01" if final else b"\x00")


def _read_full(handle, size):
    data = b""
    while len(data) < size:
        part = handle.read(size - len(data))
        if not part:
            break
        data += part
    return data


def encrypt_file(source, target, key, chunk_size=CHUNK_SIZE):
    """Encrypt ``source`` into ``target``. Returns (sha256 hex of the ENCRYPTED file, size,
    sha256 hex of the plaintext)."""
    _check_key(key)
    aes = AESGCM(bytes(key))
    base = os.urandom(8)
    header = HEADER.pack(MAGIC, VERSION, chunk_size, bytes.fromhex(fingerprint(key))[:8], base)
    cipher_hash, plain_hash, size = hashlib.sha256(), hashlib.sha256(), 0
    with open(source, "rb") as src, open(target, "wb") as dst:
        dst.write(header)
        cipher_hash.update(header)
        size += len(header)
        counter = 0
        block = _read_full(src, chunk_size)
        while True:
            following = _read_full(src, chunk_size) if len(block) == chunk_size else b""
            final = not following
            plain_hash.update(block)
            sealed = aes.encrypt(_nonce(base, counter), block, _aad(header, counter, final))
            dst.write(sealed)
            cipher_hash.update(sealed)
            size += len(sealed)
            if final:
                break
            block, counter = following, counter + 1
    return cipher_hash.hexdigest(), size, plain_hash.hexdigest()


def read_header(path):
    """(version, chunk_size, fingerprint hex of 16 chars, base nonce) of a backup file."""
    with open(path, "rb") as handle:
        raw = _read_full(handle, HEADER.size)
    if len(raw) != HEADER.size:
        raise DecryptionError("the file is too short to be a backup")
    magic, version, chunk_size, fp, base = HEADER.unpack(raw)
    if magic != MAGIC or version != VERSION or not 4096 <= chunk_size <= 64 * 1024 * 1024:
        raise DecryptionError("this is not a backup file of a supported version")
    return version, chunk_size, fp.hex(), base


def decrypt_file(source, target, key):
    """Decrypt ``source`` into ``target`` (``None`` = verify only). Returns the plaintext SHA-256.
    Raises DecryptionError on a wrong key or ANY modification; a partial target is removed."""
    _check_key(key)
    version, chunk_size, fp, base = read_header(source)
    if not hmac.compare_digest(fp, fingerprint(key)[:16]):
        raise DecryptionError("this backup was made with a different key")
    aes = AESGCM(bytes(key))
    plain_hash = hashlib.sha256()
    sealed_size = chunk_size + TAG
    out = open(target, "wb") if target else None
    try:
        with open(source, "rb") as src:
            header = _read_full(src, HEADER.size)
            counter = 0
            block = _read_full(src, sealed_size)
            if not block:
                raise DecryptionError("the backup has no data")
            while True:
                following = _read_full(src, sealed_size) if len(block) == sealed_size else b""
                final = not following
                try:
                    plain = aes.decrypt(_nonce(base, counter), block, _aad(header, counter, final))
                except InvalidTag as exc:
                    raise DecryptionError(
                        "the backup is damaged or has been tampered with (chunk "
                        f"{counter} failed authentication)"
                    ) from exc
                plain_hash.update(plain)
                if out:
                    out.write(plain)
                if final:
                    break
                block, counter = following, counter + 1
    except BaseException:
        if out:
            out.close()
            out = None
            try:
                os.remove(target)
            except OSError:
                pass
        raise
    finally:
        if out:
            out.close()
    return plain_hash.hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
