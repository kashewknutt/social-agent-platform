"""Capture screenshots (and best-effort video) for shortlisted posts only."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from ig_agent.config import MEDIA_DIR, Settings, get_settings

logger = logging.getLogger("ig_agent.media_capture")

ProgressFn = Callable[[str], None]

_VIDEO_SRC_JS = """
() => {
  const v = document.querySelector('video');
  if (!v) return '';
  const src = v.currentSrc || v.src || '';
  if (src && !src.startsWith('blob:')) return src;
  const source = v.querySelector('source');
  if (source && source.src && !source.src.startsWith('blob:')) return source.src;
  return '';
}
"""


def _slug_for_url(url: str) -> str:
    host_path = (urlparse(url).path or "post").strip("/").replace("/", "_")
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    base = re.sub(r"[^a-zA-Z0-9_-]+", "", host_path)[:48] or "post"
    return f"{base}_{digest}"


async def _try_download_video(page: Any, out_base: Path) -> Path | None:
    """Best-effort: read <video> src from the open page and download bytes."""
    try:
        src = await page.evaluate(_VIDEO_SRC_JS)
        if isinstance(src, dict):
            src = src.get("value") or src.get("result") or ""
        src = str(src or "").strip().strip('"')
        if not src or src.startswith("blob:"):
            return None
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True, trust_env=False) as client:
            resp = await client.get(src, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code >= 400 or not resp.content:
                return None
            ctype = resp.headers.get("content-type", "")
            suffix = ".webm" if "webm" in ctype else ".mp4"
            out_path = out_base.with_suffix(suffix)
            out_path.write_bytes(resp.content)
            if out_path.stat().st_size < 10_000:
                out_path.unlink(missing_ok=True)
                return None
            return out_path
    except Exception as exc:
        logger.info("Video download skipped: %s", exc)
        return None


async def capture_media_for_posts(
    posts: list[dict[str, Any]],
    *,
    settings: Settings | None = None,
    on_progress: ProgressFn | None = None,
) -> list[dict[str, Any]]:
    """Open each shortlisted post URL, screenshot, optionally download video.

    Mutates and returns the same post dicts with media_path / screenshot_path set
    when capture succeeds.
    """
    from ig_agent.browser_factory import make_browser_session, safe_kill

    cfg = settings or get_settings()
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)

    def progress(msg: str) -> None:
        logger.info(msg)
        if on_progress:
            on_progress(msg)

    targets = [p for p in posts if p.get("post_url")]
    if not targets:
        progress("No shortlisted posts with URLs to capture")
        return posts

    browser = make_browser_session(cfg, headless=True)
    try:
        await browser.start()
        for idx, post in enumerate(targets, start=1):
            url = str(post["post_url"])
            slug = _slug_for_url(url)
            progress(f"Capturing media {idx}/{len(targets)}")
            try:
                await browser.navigate_to(url)
                await asyncio.sleep(2.5)

                shot_path = MEDIA_DIR / f"{slug}.png"
                await browser.take_screenshot(path=str(shot_path), full_page=False)
                if shot_path.exists() and shot_path.stat().st_size > 0:
                    post["screenshot_path"] = str(shot_path)
                    post["media_path"] = str(shot_path)

                post_type = str(post.get("post_type") or "").lower()
                if "reel" in post_type or "/reel/" in url:
                    page = await browser.get_current_page()
                    if page is not None:
                        video_path = await _try_download_video(page, MEDIA_DIR / slug)
                        if video_path:
                            post["media_path"] = str(video_path)
                            post["video_path"] = str(video_path)
                            progress(f"Saved video for post {idx}/{len(targets)}")
                        else:
                            progress(f"Screenshot only for post {idx}/{len(targets)}")
                    else:
                        progress(f"Screenshot only for post {idx}/{len(targets)}")
                else:
                    progress(f"Screenshot saved for post {idx}/{len(targets)}")
            except Exception as exc:
                logger.warning("Capture failed for %s: %s", url, exc)
                progress(f"Capture failed for post {idx}/{len(targets)} - skipping")
                continue
    finally:
        await safe_kill(browser)

    return posts
