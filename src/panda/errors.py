"""Fixed operator diagnostics; never derive messages from exception text."""

import errno
from zipfile import BadZipFile

from panda.limits import CapacityError


class EncryptedError(ValueError):
    pass


class ImageOnlyError(ValueError):
    pass


class UnsupportedError(ValueError):
    pass


class LinkedPathError(ValueError):
    pass


def refusal_reason(error: Exception) -> str:
    if isinstance(error, CapacityError | MemoryError | RecursionError):
        return "E_CAPACITY: resource limit exceeded; split the job or reduce document size"
    if isinstance(error, EncryptedError):
        return "E_ENCRYPTED: encrypted PDF refused; provide an unencrypted text-layer copy"
    if isinstance(error, ImageOnlyError):
        return (
            "E_IMAGE_ONLY: no extractable text;"
            " provide a text-layer document; OCR is unsupported"
        )
    if isinstance(error, UnsupportedError):
        return "E_UNSUPPORTED: unsupported format or encoding; export to a supported format"
    if isinstance(error, PermissionError):
        return "E_PERMISSION: access denied; check source and destination permissions"
    if isinstance(error, OSError) and error.errno in {errno.ENOSPC, errno.EDQUOT}:
        return "E_DISK: storage full or quota exceeded; free space and use fresh destinations"
    if isinstance(error, OSError):
        return (
            "E_UNREADABLE: filesystem operation failed;"
            " check file availability and storage access"
        )
    if isinstance(error, LinkedPathError):
        return (
            "E_LINKED: a symbolic link is in the selected folder or inside it;"
            " run on the real folder (realpath FOLDER) and replace links with real files"
        )
    if isinstance(error, ValueError | BadZipFile):
        return "E_MALFORMED: invalid or unverifiable content; export a fresh supported document"
    return (
        "E_UNREADABLE: extraction or verification failed;"
        " check the document and local installation"
    )
