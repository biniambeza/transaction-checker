"""Automatic HTTPS tunnel for the Telegram Mini App dashboard using cloudflared."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import urllib.request
from pathlib import Path

import config

log = logging.getLogger(__name__)

CLOUDFLARED_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"


def get_cloudflared_path() -> Path | None:
    local = config.BASE_DIR / "cloudflared.exe"
    if local.is_file():
        return local
    which = shutil.which("cloudflared")
    if which:
        return Path(which)
    return None


def ensure_cloudflared() -> Path | None:
    path = get_cloudflared_path()
    if path:
        return path
    target = config.BASE_DIR / "cloudflared.exe"
    log.info("Downloading cloudflared for Telegram WebApp dashboard...")
    try:
        urllib.request.urlretrieve(CLOUDFLARED_URL, target)
        return target
    except Exception as e:
        log.warning("Could not download cloudflared: %s", e)
        return None


async def start_tunnel(port: int = 8080) -> tuple[asyncio.subprocess.Process | None, str | None]:
    exe = ensure_cloudflared()
    if not exe:
        return None, None

    log.info("Starting HTTPS tunnel via cloudflared for port %d...", port)
    try:
        proc = await asyncio.create_subprocess_exec(
            str(exe), "tunnel", "--url", f"http://127.0.0.1:{port}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:
        log.warning("Failed to start cloudflared process: %s", e)
        return None, None

    url = None
    for _ in range(60):
        try:
            line = await asyncio.wait_for(proc.stderr.readline(), timeout=15.0)
        except asyncio.TimeoutError:
            break
        if not line:
            break
        text = line.decode("utf-8", "ignore")
        m = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", text)
        if m:
            url = m.group(0).strip()
            break

    if url:
        log.info("✅ Tunnel active: %s", url)
    else:
        log.warning("Could not obtain trycloudflare URL from cloudflared.")
    return proc, url


async def close_tunnel(proc: asyncio.subprocess.Process | None) -> None:
    if not proc:
        return
    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=3.0)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass

