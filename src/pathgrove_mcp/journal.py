"""Read-only NTFS change journal access for fast restarts on Windows.

Startup works like Everything's: remember where the volume's USN journal stood
when the index matched the disk, then on restart read only the records written
since and revisit the directories they name. FSCTL_READ_UNPRIVILEGED_USN_JOURNAL
needs no administrator rights but omits filenames, so records are mapped to
paths through directory file IDs that the index stores while scanning. This
module never creates, resizes or deletes a journal.
"""

from __future__ import annotations

import ctypes
import functools
import os
import struct
from collections.abc import Iterator
from dataclasses import dataclass

FSCTL_QUERY_USN_JOURNAL = 0x000900F4
FSCTL_READ_USN_JOURNAL = 0x000900BB
FSCTL_READ_UNPRIVILEGED_USN_JOURNAL = 0x000903AB  # Windows 10 1709+, no admin rights.
FILE_ATTRIBUTE_DIRECTORY = 0x10
USN_REASON_FILE_DELETE = 0x200
_GENERIC_READ = 0x80000000
_SHARE_ALL = 0x7
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_ERROR_INVALID_FUNCTION = 1
_BUFFER_BYTES = 1 << 20
# V2 records carry 64-bit file IDs, V3 records 128-bit IDs.
# Offsets: (file ID, parent ID, reason, attributes, minimum record length).
_LAYOUTS = {2: (8, 16, 40, 52, 60), 3: (8, 24, 56, 68, 76)}


class JournalUnavailable(OSError):
    """The journal cannot prove what changed; the caller must walk directories."""


@dataclass(frozen=True)
class Checkpoint:
    volume: str
    journal_id: int
    usn: int


def parse_records(data: bytes) -> Iterator[tuple[int, int, int, int]]:
    """Yield (file_id, parent_id, reason, attributes) from records after the 8-byte cursor.

    Malformed lengths and unknown versions raise instead of silently skipping
    changes, which would leave stale entries behind.
    """
    position = 0
    while position < len(data):
        if len(data) - position < 8:
            raise JournalUnavailable("Truncated USN record header")
        length, major = struct.unpack_from("<IH", data, position)
        layout = _LAYOUTS.get(major)
        if layout is None:
            raise JournalUnavailable(f"Unsupported USN record version {major}")
        file_offset, parent_offset, reason_offset, attributes_offset, minimum = layout
        if length < minimum or length > len(data) - position:
            raise JournalUnavailable("Invalid USN record length")
        width = parent_offset - file_offset
        record = data[position : position + length]
        yield (
            int.from_bytes(record[file_offset : file_offset + width], "little"),
            int.from_bytes(record[parent_offset : parent_offset + width], "little"),
            struct.unpack_from("<I", record, reason_offset)[0],
            struct.unpack_from("<I", record, attributes_offset)[0],
        )
        position += length


class Changes:
    """Directories named by journal records, and where changed folders live now.

    Every record names its parent directory; a folder's records also name the
    folder. A folder only arrives somewhere (created, moved or renamed in, or
    replacing another) through a record of its own naming its new parent, and a
    deleted folder's last record says so. So only subfolders listed in
    `arrivals()` can carry an ID the index has not recorded at their path.
    """

    def __init__(self) -> None:
        self.directories: set[int] = set()
        self._folders: dict[int, int | None] = {}  # Folder ID -> current parent, None if gone.

    def add(self, data) -> None:
        for file_id, parent_id, reason, attributes in parse_records(data):
            self.directories.add(parent_id)
            if attributes & FILE_ATTRIBUTE_DIRECTORY:
                self.directories.add(file_id)
                self._folders[file_id] = None if reason & USN_REASON_FILE_DELETE else parent_id

    def arrivals(self) -> dict[int, set[int]]:
        """Directory ID -> IDs of changed folders that now live directly in it."""
        result: dict[int, set[int]] = {}
        for folder, parent in self._folders.items():
            if parent is not None:
                result.setdefault(parent, set()).add(folder)
        return result


@functools.lru_cache(maxsize=1)
def _kernel():
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    kernel.GetVolumePathNameW.restype = wintypes.BOOL
    kernel.GetVolumeNameForVolumeMountPointW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    kernel.GetVolumeNameForVolumeMountPointW.restype = wintypes.BOOL
    kernel.GetVolumeInformationW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    kernel.GetVolumeInformationW.restype = wintypes.BOOL
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.DeviceIoControl.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    kernel.DeviceIoControl.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


def _failure(message: str) -> JournalUnavailable:
    error = ctypes.get_last_error()
    return JournalUnavailable(error, f"{message}: {ctypes.FormatError(error).strip()}")


class Volume:
    """A short-lived read handle on the NTFS volume that holds `path`."""

    def __init__(self, path: str):
        if os.name != "nt":
            raise JournalUnavailable("The NTFS change journal is only available on Windows")
        kernel = _kernel()
        mount = ctypes.create_unicode_buffer(32768)
        if not kernel.GetVolumePathNameW(path, mount, len(mount)):
            raise _failure(f"Cannot find the volume of {path}")
        name = ctypes.create_unicode_buffer(64)
        if not kernel.GetVolumeNameForVolumeMountPointW(mount.value, name, len(name)):
            raise _failure(f"Cannot name the volume mounted at {mount.value}")
        filesystem = ctypes.create_unicode_buffer(64)
        if not kernel.GetVolumeInformationW(
            mount.value, None, 0, None, None, None, filesystem, len(filesystem)
        ):
            raise _failure(f"Cannot read the filesystem type of {mount.value}")
        if filesystem.value != "NTFS":
            # Directory IDs from os.stat match journal IDs only on NTFS.
            raise JournalUnavailable(f"{mount.value} uses {filesystem.value}, not NTFS")
        self.name = name.value
        # Opening the volume GUID path as a directory needs no administrator rights,
        # unlike \\.\C: (the approach Microsoft BuildXL uses).
        self._handle = kernel.CreateFileW(
            self.name,
            _GENERIC_READ,
            _SHARE_ALL,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
        if self._handle == ctypes.c_void_p(-1).value:
            raise _failure(f"Cannot open volume {self.name}")

    def __enter__(self) -> Volume:
        return self

    def __exit__(self, *exc_info) -> None:
        _kernel().CloseHandle(self._handle)

    def _control(self, code: int, request: bytes | None, output) -> int:
        from ctypes import wintypes

        returned = wintypes.DWORD()
        if not _kernel().DeviceIoControl(
            self._handle,
            code,
            request,
            len(request) if request else 0,
            output,
            len(output),
            ctypes.byref(returned),
            None,
        ):
            raise _failure(f"Change journal request {code:#x} failed on {self.name}")
        return returned.value

    def _bounds(self) -> tuple[int, int, int]:
        output = ctypes.create_string_buffer(80)
        if self._control(FSCTL_QUERY_USN_JOURNAL, None, output) < 24:
            raise JournalUnavailable("Truncated USN journal information")
        journal_id, first_usn, next_usn = struct.unpack_from("<Qqq", output.raw)
        return journal_id, first_usn, next_usn

    def position(self) -> Checkpoint:
        """The current end of the journal; capture it before reading the disk."""
        journal_id, _first, next_usn = self._bounds()
        return Checkpoint(self.name, journal_id, next_usn)

    def changed_directories(
        self, since: Checkpoint, changes: Changes | None = None
    ) -> tuple[Changes, Checkpoint]:
        """Changes recorded since `since` (added to `changes`), and the next checkpoint."""
        journal_id, first_usn, next_usn = self._bounds()
        if since.volume != self.name:
            raise JournalUnavailable("The indexed root moved to another volume")
        if since.journal_id != journal_id:
            raise JournalUnavailable("The change journal was recreated")
        # FirstUsn moves as old records are discarded; reads from before it are not
        # reliably refused (a start of 0 silently skips ahead), so check explicitly.
        if not first_usn <= since.usn <= next_usn:
            raise JournalUnavailable("The change journal no longer reaches the checkpoint")
        output = ctypes.create_string_buffer(_BUFFER_BYTES)
        code = FSCTL_READ_UNPRIVILEGED_USN_JOURNAL
        changes = Changes() if changes is None else changes
        cursor = since.usn
        while True:
            # READ_USN_JOURNAL_DATA_V1: return at once, every reason, V2 or V3 records.
            request = struct.pack("<qIIQQQHH4x", cursor, 0xFFFFFFFF, 0, 0, 0, journal_id, 2, 3)
            try:
                returned = self._control(code, request, output)
            except JournalUnavailable as exc:
                if exc.errno != _ERROR_INVALID_FUNCTION or code == FSCTL_READ_USN_JOURNAL:
                    raise
                code = FSCTL_READ_USN_JOURNAL  # Older Windows: works when elevated.
                continue
            if returned < 8:
                raise JournalUnavailable("Truncated change journal buffer")
            data = memoryview(ctypes.string_at(output, returned))
            following = struct.unpack_from("<q", data)[0]
            changes.add(data[8:])
            if returned == 8 or following <= cursor:
                return changes, Checkpoint(self.name, journal_id, following)
            cursor = following
