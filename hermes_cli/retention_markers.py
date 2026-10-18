"""No-follow marker inspection with a bounded regular-file readability probe."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import errno
import os
from pathlib import Path
import stat


class MarkerStatus(Enum):
    ABSENT = "absent"
    PRESENT = "present"
    UNSAFE = "unsafe"


@dataclass(frozen=True)
class MarkerInspection:
    status: MarkerStatus
    reason: str


class RetainAll(ValueError):
    """Inspection uncertainty retains the original, before bundle selection."""


def _identity(info):
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _open_directory_chain(root: Path) -> list[int]:
    """Pin every ancestor from the filesystem root without following links."""
    descriptors = []
    try:
        descriptors.append(os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
        for part in root.parts[1:]:
            descriptors.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                       dir_fd=descriptors[-1]))
        return descriptors
    except BaseException:
        for fd in reversed(descriptors):
            os.close(fd)
        raise


def _chain_stable(root: Path, descriptors: list[int]) -> bool:
    # Revalidate all parent entries against pinned descriptors. This detects
    # observed substitution; it does not prove exclusive custody against writers.
    for index, part in enumerate(root.parts[1:], 1):
        observed = os.stat(part, dir_fd=descriptors[index - 1], follow_symlinks=False)
        if _identity(observed) != _identity(os.fstat(descriptors[index])):
            return False
    return True


def inspect_keep_bundle(workspace: Path, *, max_bytes: int = 4096) -> MarkerInspection:
    """Only a verified missing leaf is ABSENT; all uncertainty is typed UNSAFE.

    Inspect type before opening and use nonblocking/no-follow semantics. Only a
    verified regular descriptor gets a bounded read; specials are never opened.
    """
    descriptors = []
    marker_fd = None
    try:
        if not all(hasattr(os, flag) for flag in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")):
            return MarkerInspection(MarkerStatus.UNSAFE, "unsupported no-follow inspection")
        root = Path(os.path.abspath(workspace))
        descriptors = _open_directory_chain(root)
        parent = descriptors[-1]
        try:
            before = os.stat("KEEP-BUNDLE", dir_fd=parent, follow_symlinks=False)
        except OSError as exc:
            if exc.errno == errno.ENOENT and _chain_stable(root, descriptors):
                return MarkerInspection(MarkerStatus.ABSENT, "verified absent leaf")
            raise
        if (not stat.S_ISREG(before.st_mode) or not before.st_mode & stat.S_IRUSR
                or before.st_size > max_bytes):
            return MarkerInspection(MarkerStatus.UNSAFE, "marker is not a bounded readable regular file")
        marker_fd = os.open("KEEP-BUNDLE", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        after = os.fstat(marker_fd)
        if not stat.S_ISREG(after.st_mode) or _identity(before) != _identity(after):
            return MarkerInspection(MarkerStatus.UNSAFE, "marker identity changed before read")
        # Readability includes actual I/O, not just successful open or mode bits.
        # Contents carry no instructions or authority and are never interpreted.
        if len(os.read(marker_fd, max_bytes + 1)) != before.st_size:
            return MarkerInspection(MarkerStatus.UNSAFE, "marker changed or exceeded read bound")
        after = os.fstat(marker_fd)
        final = os.stat("KEEP-BUNDLE", dir_fd=parent, follow_symlinks=False)
        if (not stat.S_ISREG(after.st_mode) or _identity(before) != _identity(after)
                or _identity(after) != _identity(final) or not _chain_stable(root, descriptors)):
            return MarkerInspection(MarkerStatus.UNSAFE, "marker or ancestor identity changed")
        return MarkerInspection(MarkerStatus.PRESENT, "verified regular readable marker")
    except (OSError, ValueError, NotImplementedError):
        return MarkerInspection(MarkerStatus.UNSAFE, "marker or ancestor inspection failed")
    finally:
        if marker_fd is not None:
            os.close(marker_fd)
        for fd in reversed(descriptors):
            os.close(fd)
