"""Look at what is REALLY in a backup destination: every file, and the key each one names.

Used by ``abort`` and ``prune`` (decisions: never delete a key a backup still needs). The database
history alone is not enough: after a restore from an older backup it lacks later backups, and a
backup can be uploaded and the process interrupted before it is recorded. So the destination is
enumerated completely (or the operation refuses), every file's header is read, and a file that
cannot be read as a backup is never assumed to be irrelevant.

Header fingerprints are not themselves authenticated until decryption, but a header altered to name
another key makes the file undecryptable with any key (the header is additional data of every
chunk), so a forged fingerprint cannot hide a file that really needs a key.
"""

from collections import Counter
from dataclasses import dataclass, field

from . import crypto
from .destinations import DestinationError


class ScanError(Exception):
    """The destination could not be fully enumerated. The message is safe to show."""


@dataclass
class ScanEntry:
    name: str
    remote_id: str
    trashed: bool = False
    fingerprint: str | None = None  # 16 hex characters, None when the header could not be read
    problem: str = ""  # why it could not be inspected ("" when it is a readable backup)


@dataclass
class Scan:
    entries: list = field(default_factory=list)

    @property
    def problems(self):
        return [e for e in self.entries if e.problem]

    @property
    def readable(self):
        return [e for e in self.entries if not e.problem]

    @property
    def duplicate_names(self):
        counts = Counter(e.name for e in self.entries)
        return sorted(name for name, n in counts.items() if n > 1)

    def using(self, fingerprint):
        return [e for e in self.readable if e.fingerprint == fingerprint]

    def fingerprints(self):
        return sorted({e.fingerprint for e in self.readable})

    def remote_ids(self):
        return {e.remote_id for e in self.entries}


def scan_destination(destination):
    """A ``Scan`` of every entry, or ``ScanError`` when the destination cannot be fully listed.
    Entries that cannot be safely inspected are returned with ``problem`` set (callers refuse)."""
    if not hasattr(destination, "enumerate_files") or not hasattr(destination, "read_head"):
        raise ScanError("this destination cannot be enumerated")
    try:
        found = destination.enumerate_files()
    except DestinationError as exc:
        raise ScanError(f"the destination could not be fully listed: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - any listing failure is a failure to enumerate
        raise ScanError(
            f"the destination could not be fully listed ({type(exc).__name__})"
        ) from exc
    scan = Scan()
    for item in found:
        entry = ScanEntry(item.name, item.remote_id, trashed=item.trashed)
        if item.is_folder:
            entry.problem = "a folder, not a backup file"
        else:
            try:
                head = destination.read_head(item, crypto.HEADER.size)
                entry.fingerprint = crypto.parse_header(head)[2]
            except crypto.DecryptionError as exc:
                entry.problem = str(exc)
            except DestinationError as exc:
                entry.problem = str(exc)
            except Exception as exc:  # noqa: BLE001
                entry.problem = f"the header could not be read ({type(exc).__name__})"
        scan.entries.append(entry)
    return scan
