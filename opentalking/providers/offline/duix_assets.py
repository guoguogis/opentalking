"""Publish job inputs so an offline renderer can download them.

DUIX fetches ``audio_url`` / ``video_url`` itself. Its container has no internet
egress (it cannot even reach our public hostname), so the URLs must point at an
address inside its own network - in practice the internal interface of this host,
configured through ``OPENTALKING_DUIX_ASSET_BASE_URL``.

Files are copied into ``<duix_asset_dir>/<token>/`` and served by
``GET /duix/assets/{token}/{name}``. The token is 24 hex chars of randomness, so
the URL is unguessable without also needing an API key; entries expire after
``duix_asset_ttl_sec`` and are swept on every publish.
"""

from __future__ import annotations

import logging
import secrets
import shutil
import time
from pathlib import Path
from urllib.parse import quote

logger = logging.getLogger(__name__)

_MAX_NAME_LEN = 64


def resolve_asset_dir(settings: object) -> Path:
    """Directory holding published inputs (``<data dir>/duix_assets`` by default)."""
    explicit = str(getattr(settings, "duix_asset_dir", "") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    data_dir = str(getattr(settings, "data_dir", "") or "").strip() or "./data"
    return Path(data_dir).expanduser() / "duix_assets"


def resolve_asset_base_url(settings: object) -> str:
    return str(getattr(settings, "duix_asset_base_url", "") or "").strip().rstrip("/")


def _safe_name(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in name)
    cleaned = cleaned.strip("._") or "asset"
    return cleaned[:_MAX_NAME_LEN]


def sweep_expired(root: Path, ttl_sec: int) -> int:
    """Remove asset folders older than ``ttl_sec``; returns how many were removed."""
    if not root.is_dir():
        return 0
    cutoff = time.time() - max(60, int(ttl_sec))
    removed = 0
    for child in root.iterdir():
        if not child.is_dir():
            continue
        try:
            if child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


class DuixAssetPublisher:
    """Copies inputs into the public folder and builds their download URLs."""

    def __init__(self, settings: object) -> None:
        self.settings = settings
        self.root = resolve_asset_dir(settings)
        self.base_url = resolve_asset_base_url(settings)
        try:
            self.ttl = int(getattr(settings, "duix_asset_ttl_sec", 3600) or 3600)
        except (TypeError, ValueError):
            self.ttl = 3600

    def new_token(self) -> str:
        return secrets.token_hex(12)

    def publish(self, token: str, source: Path, name: str) -> str:
        """Copy ``source`` into the token folder and return its absolute URL."""
        if not self.base_url:
            raise RuntimeError(
                "OPENTALKING_DUIX_ASSET_BASE_URL is not configured; the renderer "
                "cannot download job inputs without it"
            )
        safe = _safe_name(name)
        target_dir = self.root / token
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / safe
        if source.resolve() != target.resolve():
            shutil.copyfile(source, target)
        sweep_expired(self.root, self.ttl)
        url = f"{self.base_url}/duix/assets/{token}/{quote(safe)}"
        logger.info("DUIX asset published: %s (%d bytes)", url, target.stat().st_size)
        return url

    def cleanup(self, token: str) -> None:
        shutil.rmtree(self.root / token, ignore_errors=True)
