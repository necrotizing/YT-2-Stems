# updater.py — keeps the volatile yt-dlp dependency current on launch.
#
# Rationale: YouTube/SoundCloud change their sites often and break older
# yt-dlp releases (e.g. the "page needs to be reloaded" / SABR errors). The
# heavy ML stack (torch, demucs, essentia, numpy…) is deliberately pinned in
# requirements.txt for reproducibility and is NOT touched here — upgrading it
# blindly causes more breakage than it fixes. Only yt-dlp is refreshed.

import sys
import subprocess
from dataclasses import dataclass
from enum import Enum
from importlib import metadata

# The single package we keep on the bleeding edge.
VOLATILE_PACKAGE = "yt-dlp"

# Network timeout for the pip upgrade, in seconds. Kept generous because pip
# may need to build/download, but bounded so a stalled network never hangs
# the whole app on launch.
UPGRADE_TIMEOUT_SECONDS = 120


class UpdateStatus(Enum):
    """Outcome of an update attempt. No exceptions leak to the caller."""
    UP_TO_DATE = "up_to_date"
    UPDATED = "updated"
    OFFLINE = "offline"        # network / pip failure — non-fatal, keep going
    ERROR = "error"            # unexpected failure — non-fatal, keep going


@dataclass(frozen=True)
class UpdateResult:
    """Immutable summary of an update attempt, safe to pass around threads."""
    status: UpdateStatus
    old_version: str | None
    new_version: str | None
    message: str


def _installed_version(package: str) -> str | None:
    """Return the installed version of *package*, or None if not found."""
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return None
    except Exception:
        # Never let version probing crash launch.
        return None


def update_ytdlp(timeout: int = UPGRADE_TIMEOUT_SECONDS) -> UpdateResult:
    """
    Upgrade yt-dlp to the latest release using the *current* interpreter's pip.

    Returns an UpdateResult describing what happened. All failure modes
    (offline, pip missing, timeout) are captured in the result rather than
    raised, so the caller can decide whether to continue.
    """
    old_version = _installed_version(VOLATILE_PACKAGE)

    cmd = [
        sys.executable, "-m", "pip", "install", "--upgrade",
        "--disable-pip-version-check", VOLATILE_PACKAGE,
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return UpdateResult(
            status=UpdateStatus.OFFLINE,
            old_version=old_version,
            new_version=old_version,
            message=(
                f"yt-dlp update timed out after {timeout}s — "
                f"continuing with current version ({old_version or 'unknown'})."
            ),
        )
    except Exception as exc:
        return UpdateResult(
            status=UpdateStatus.ERROR,
            old_version=old_version,
            new_version=old_version,
            message=f"Could not run pip to update yt-dlp: {exc}",
        )

    if proc.returncode != 0:
        # Most commonly: no network. Surface the tail of pip's output so the
        # user has context, but treat it as non-fatal.
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else "no output"
        return UpdateResult(
            status=UpdateStatus.OFFLINE,
            old_version=old_version,
            new_version=old_version,
            message=(
                f"yt-dlp update skipped (pip exit {proc.returncode}: {tail}) — "
                f"continuing with current version ({old_version or 'unknown'})."
            ),
        )

    new_version = _installed_version(VOLATILE_PACKAGE)

    if old_version and new_version and old_version != new_version:
        return UpdateResult(
            status=UpdateStatus.UPDATED,
            old_version=old_version,
            new_version=new_version,
            message=f"yt-dlp updated: {old_version} → {new_version}",
        )

    return UpdateResult(
        status=UpdateStatus.UP_TO_DATE,
        old_version=old_version,
        new_version=new_version,
        message=f"yt-dlp up to date ({new_version or 'unknown'})",
    )
