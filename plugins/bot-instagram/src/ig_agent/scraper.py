"""Non-LLM Instagram research scraping (grid URLs + post detail)."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from typing import Any

from ig_agent.config import Settings, get_settings
from ig_agent.posts import canonicalize_ig_url, merge_posts
from ig_agent.scripted_actions import (
    ScriptedActionError,
    _check_blockers,
    _dismiss_known_dialogs,
    _ensure_page,
)

logger = logging.getLogger("ig_agent.scraper")

_REEL_META_JS = """
() => {
  const ogUrl = document.querySelector('meta[property="og:url"]')?.content || '';
  const canonical = document.querySelector('link[rel="canonical"]')?.href || '';
  const og = document.querySelector('meta[property="og:description"]');
  const caption = og ? (og.getAttribute('content') || '') : '';
  let username = '';
  for (const a of document.querySelectorAll('a[href^="/"]')) {
    const h = a.getAttribute('href') || '';
    const m = h.match(/^\\/([a-zA-Z0-9._]+)\\/?$/);
    if (m && !['p','reel','reels','explore','accounts','direct'].includes(m[1])) {
      username = m[1];
      break;
    }
  }
  return { og_url: ogUrl, canonical, caption: caption.slice(0, 500), username };
}
"""

_GRID_JS = """
() => {
  const out = [];
  const seen = new Set();
  const add = (href, alt) => {
    if (!href) return;
    if (href.startsWith('/')) href = 'https://www.instagram.com' + href;
    try {
      href = new URL(href, 'https://www.instagram.com').href.split('?')[0].replace(/\\/$/, '');
    } catch (e) { return; }
    if (!href.includes('/p/') && !href.includes('/reel/')) return;
    if (seen.has(href)) return;
    seen.add(href);
    const og = document.querySelector('meta[property="og:description"]');
    const ogCap = og ? (og.getAttribute('content') || '') : '';
    out.push({
      post_url: href,
      caption: (alt || ogCap || '').slice(0, 500),
      post_type: href.includes('/reel/') ? 'reel' : 'post',
    });
  };
  const ogUrl = document.querySelector('meta[property="og:url"]')?.content || '';
  const canonical = document.querySelector('link[rel="canonical"]')?.href || '';
  add(ogUrl, '');
  add(canonical, '');
  const selectors = [
    'a[href*="/p/"]', 'a[href*="/reel/"]',
    '[role="link"][href*="/p/"]', '[role="link"][href*="/reel/"]',
  ];
  for (const sel of selectors) {
    for (const a of document.querySelectorAll(sel)) {
      const href = a.getAttribute('href') || '';
      const img = a.querySelector('img');
      const alt = img ? (img.getAttribute('alt') || '') : '';
      add(href, alt);
    }
  }
  const path = window.location.pathname || '';
  if (/^\\/(p|reel)\\/[^/]+/.test(path)) {
    add(window.location.href, '');
  }
  return out;
}
"""

_DETAIL_JS = """
() => {
  const og = document.querySelector('meta[property="og:description"]');
  const caption = og ? (og.getAttribute('content') || '') : '';
  let username = '';
  const pathMatch = window.location.pathname.match(/^\\/(p|reel)\\/[^/]+\\/?$/);
  const headerLink = document.querySelector('header a[href^="/"]');
  if (headerLink) {
    const h = headerLink.getAttribute('href') || '';
    const m = h.match(/^\\/([^/]+)\\/?$/);
    if (m && !['p','reel','explore','accounts'].includes(m[1])) username = m[1];
  }
  if (!username) {
    const any = document.querySelector('a[href^="/"][role="link"]');
    if (any) {
      const h = any.getAttribute('href') || '';
      const m = h.match(/^\\/([a-zA-Z0-9._]+)\\/?$/);
      if (m) username = m[1];
    }
  }
  const body = document.body ? document.body.innerText : '';
  const likesMatch = body.match(/([\\d,.]+[KMB]?)\\s+likes/i);
  const commentsMatch = body.match(/View all\\s+([\\d,.]+[KMB]?)\\s+comments/i)
    || body.match(/([\\d,.]+[KMB]?)\\s+comments/i);
  return {
    caption: caption,
    username: username,
    profile_url: username ? 'https://www.instagram.com/' + username + '/' : null,
    likes: likesMatch ? likesMatch[1] : null,
    comments_count: commentsMatch ? commentsMatch[1] : null,
  };
}
"""


class ScrapeError(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail or reason
        super().__init__(f"{reason}: {self.detail}")


def _parse_eval_result(raw: Any) -> Any:
    """browser-use Page.evaluate returns JSON strings for objects/arrays."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, (list, dict, bool, int, float)):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return raw


async def _scroll_feed(page: Any, passes: int = 3) -> None:
    """Scroll to trigger lazy-loaded grid tiles."""
    for _ in range(max(1, passes)):
        await page.evaluate("() => { window.scrollBy(0, Math.min(1200, window.innerHeight * 1.2)); }")
        await asyncio.sleep(1.0 + random.uniform(0.2, 0.5))


async def harvest_posts_from_page(page: Any) -> list[dict[str, Any]]:
    """Extract post URLs from whatever is currently visible (no navigation)."""
    raw_items: list[dict[str, Any]] = []

    try:
        if hasattr(page, "get_url"):
            cur = canonicalize_ig_url(await page.get_url())
            if cur:
                raw_items.append({"post_url": cur, "caption": "", "post_type": "reel" if "/reel/" in cur else "post"})
    except Exception:
        pass

    parsed = _parse_eval_result(await page.evaluate(_GRID_JS))
    if isinstance(parsed, list):
        raw_items.extend(p for p in parsed if isinstance(p, dict))

    meta = _parse_eval_result(await page.evaluate(_REEL_META_JS))
    if isinstance(meta, dict):
        for key in ("og_url", "canonical"):
            url = canonicalize_ig_url(str(meta.get(key) or ""))
            if url:
                raw_items.append(
                    {
                        "post_url": url,
                        "caption": meta.get("caption") or "",
                        "username": meta.get("username"),
                        "post_type": "reel" if "/reel/" in url else "post",
                    }
                )

    return _normalize_posts(raw_items, limit=50)


async def harvest_posts_from_browser(browser: Any) -> list[dict[str, Any]]:
    """Best-effort DOM harvest from the active browser tab."""
    try:
        page = await _ensure_page(browser)
    except Exception as exc:
        logger.debug("harvest_posts_from_browser: no page (%s)", exc)
        return []
    try:
        return await harvest_posts_from_page(page)
    except Exception as exc:
        logger.debug("harvest_posts_from_page failed: %s", exc)
        return []


async def _navigate_and_get_page(browser: Any, url: str) -> Any:
    page = await _ensure_page(browser)
    await browser.navigate_to(url)
    await asyncio.sleep(1.0 + random.uniform(0.1, 0.3))
    await _dismiss_known_dialogs(page)
    try:
        await _check_blockers(page)
    except ScriptedActionError as exc:
        raise ScrapeError(exc.reason, exc.detail) from exc
    return page


def _normalize_posts(raw: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        url = str(item.get("post_url") or "").split("?")[0].rstrip("/")
        if not url or "/p/" not in url and "/reel/" not in url:
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(
            {
                "post_url": url,
                "caption": (item.get("caption") or "")[:500],
                "username": item.get("username"),
                "profile_url": item.get("profile_url"),
                "post_type": "reel" if "/reel/" in url else "post",
                "likes": item.get("likes"),
                "comments_count": item.get("comments_count"),
                "views": item.get("views"),
                "liked": bool(item.get("liked")),
                "followed": bool(item.get("followed")),
            }
        )
        if len(out) >= limit:
            break
    return out


async def scrape_explore_candidates(
    browser: Any,
    limit: int,
    *,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    page = await _navigate_and_get_page(browser, "https://www.instagram.com/explore/")
    await _scroll_feed(page, passes=4)
    posts = await harvest_posts_from_page(page)
    posts = _normalize_posts([p for p in posts], limit)
    if not posts:
        raise ScrapeError("no_candidates", "Explore grid returned 0 post URLs")
    return posts


async def scrape_reels_candidates(
    browser: Any,
    limit: int,
    *,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    page = await _navigate_and_get_page(browser, "https://www.instagram.com/reels/")
    await asyncio.sleep(1.5)
    posts = await harvest_posts_from_page(page)
    posts = _normalize_posts([p for p in posts], limit)
    if not posts:
        raise ScrapeError("no_candidates", "Reels feed returned 0 post URLs")
    return posts


async def scrape_hashtag_candidates(
    browser: Any,
    hashtag: str,
    limit: int,
    *,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    tag = re.sub(r"[^a-zA-Z0-9_]", "", (hashtag or "").lstrip("#"))
    if not tag:
        raise ScrapeError("no_candidates", "Empty hashtag")
    url = f"https://www.instagram.com/explore/tags/{tag}/"
    page = await _navigate_and_get_page(browser, url)
    await _scroll_feed(page, passes=3)
    posts = await harvest_posts_from_page(page)
    posts = _normalize_posts([p for p in posts], limit)
    if not posts:
        raise ScrapeError("no_candidates", f"Hashtag #{tag} returned 0 post URLs")
    from ig_agent.hashtag_rotation import record_hashtag_search

    record_hashtag_search(tag, source="hashtag_grid")
    return posts


async def scrape_post_detail(
    browser: Any,
    post_url: str,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    page = await _navigate_and_get_page(browser, post_url)
    detail = _parse_eval_result(await page.evaluate(_DETAIL_JS))
    if not isinstance(detail, dict):
        detail = {}
    return {
        "post_url": post_url.split("?")[0].rstrip("/"),
        "caption": detail.get("caption") or "",
        "username": detail.get("username"),
        "profile_url": detail.get("profile_url"),
        "likes": detail.get("likes"),
        "comments_count": detail.get("comments_count"),
        "post_type": "reel" if "/reel/" in post_url else "post",
    }


async def scripted_reels_ingest(
    browser: Any,
    *,
    limit: int = 4,
    engage_live: bool = True,
    settings: Settings | None = None,
    on_progress: Any | None = None,
    controller: Any | None = None,
    run_id: str | None = None,
    should_stop: Any | None = None,
) -> list[dict[str, Any]]:
    """Scroll the Reels feed, capture each reel, like + optional live comment."""
    from ig_agent.ingest_comment_gate import prompt_and_post_ingest_comment
    from ig_agent.safety import can_perform
    from ig_agent.scripted_actions import scripted_follow, scripted_like_current

    cfg = settings or get_settings()
    page = await _navigate_and_get_page(browser, "https://www.instagram.com/reels/")
    await asyncio.sleep(2.0 + random.uniform(0.2, 0.5))

    collected: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    stagnant = 0
    max_steps = max(limit * 4, 12)

    for _step in range(max_steps):
        if len(collected) >= limit:
            break
        if should_stop and should_stop():
            break
        await asyncio.sleep(1.0 + random.uniform(0.2, 0.4))
        batch = await harvest_posts_from_page(page)
        current: dict[str, Any] | None = None
        for candidate in batch:
            url = canonicalize_ig_url(str(candidate.get("post_url") or ""))
            if url and url not in seen_urls:
                current = {**candidate, "post_url": url}
                break
        if current is None and batch:
            url = canonicalize_ig_url(str(batch[0].get("post_url") or ""))
            if url and url not in seen_urls:
                current = {**batch[0], "post_url": url}

        if current:
            url = str(current["post_url"])
            seen_urls.add(url)
            if engage_live and can_perform("like", cfg):
                try:
                    res = await scripted_like_current(browser, settings=cfg)
                    current["liked"] = res.ok
                except Exception as exc:
                    logger.debug("reels like failed: %s", exc)
            if engage_live and current.get("username") and can_perform("follow", cfg):
                try:
                    res = await scripted_follow(
                        browser,
                        profile_url=current.get("profile_url"),
                        username=current.get("username"),
                        settings=cfg,
                    )
                    current["followed"] = res.ok
                except Exception as exc:
                    logger.debug("reels follow skipped: %s", exc)
            current = await prompt_and_post_ingest_comment(
                browser,
                current,
                controller=controller,
                run_id=run_id,
                settings=cfg,
                on_progress=on_progress,
                should_stop=should_stop,
            )
            collected = merge_posts(collected, [current])  # type: ignore[arg-type]
            stagnant = 0
            if on_progress:
                cap = (current.get("caption") or "")[:60]
                on_progress(f"Reels ingest {len(collected)}/{limit}: {cap or url[:48]}")
        else:
            stagnant += 1

        if stagnant >= 5:
            break

        try:
            await page.press("ArrowDown")
        except Exception:
            await page.evaluate("() => { window.scrollBy(0, window.innerHeight); }")
        await asyncio.sleep(0.7 + random.uniform(0.1, 0.25))

    if not collected:
        raise ScrapeError("no_candidates", "Reels scroll pass collected 0 posts")
    return collected[:limit]


async def scrape_research_batch(
    browser: Any,
    *,
    hashtags: list[str] | None = None,
    limit: int = 5,
    engage_live: bool = True,
    settings: Settings | None = None,
    on_progress: Any | None = None,
    controller: Any | None = None,
    run_id: str | None = None,
    should_stop: Any | None = None,
) -> list[dict[str, Any]]:
    """Scrape candidates + enrich detail; optionally like/follow via scripted actions."""
    from ig_agent.safety import can_perform
    from ig_agent.scripted_actions import scripted_follow, scripted_like

    cfg = settings or get_settings()
    tags = hashtags or []
    candidates: list[dict[str, Any]] = []
    errors: list[str] = []

    # Fresh hashtag first (rotated in runtime) — avoid repeating recent tags.
    if tags:
        tag = tags[0]
        try:
            if on_progress:
                from ig_agent.hashtag_rotation import normalize_hashtag

                on_progress(f"Hashtag search #{normalize_hashtag(tag)}…")
            batch = await scrape_hashtag_candidates(browser, tag, limit, settings=cfg)
            candidates.extend(batch)
            if on_progress and batch:
                on_progress(f"Hashtag #{tag.lstrip('#')} → {len(batch)} post URL(s)")
        except ScrapeError as exc:
            msg = f"#{tag.lstrip('#')}: {exc.detail}"
            errors.append(msg)
            logger.warning("Hashtag scrape failed for %s: %s", tag, exc)

    if not candidates and engage_live:
        try:
            return await scripted_reels_ingest(
                browser,
                limit=limit,
                engage_live=True,
                settings=cfg,
                on_progress=on_progress,
                controller=controller,
                run_id=run_id,
                should_stop=should_stop,
            )
        except ScrapeError as exc:
            logger.warning("Reels ingest failed, trying explore grid: %s", exc)

    if not candidates:
        for source, fn in (
            ("explore", scrape_explore_candidates),
            ("reels", scrape_reels_candidates),
        ):
            try:
                candidates = await fn(browser, limit, settings=cfg)
                if candidates:
                    break
            except ScrapeError as exc:
                errors.append(f"{source}: {exc.detail}")
                logger.warning("%s scrape failed: %s", source, exc)

    # Dedupe and cap
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for p in candidates:
        u = p.get("post_url") or ""
        if u and u not in seen:
            seen.add(u)
            unique.append(p)
        if len(unique) >= limit:
            break

    enriched: list[dict[str, Any]] = []
    for post in unique:
        url = str(post.get("post_url") or "")
        if not url:
            continue
        try:
            detail = await scrape_post_detail(browser, url, settings=cfg)
            merged = {**post, **{k: v for k, v in detail.items() if v}}
        except ScrapeError:
            merged = dict(post)
        liked = False
        followed = False
        if engage_live and can_perform("like", cfg):
            try:
                res = await scripted_like(browser, url, settings=cfg)
                liked = res.ok
            except Exception as exc:
                logger.debug("scripted like during scrape failed: %s", exc)
        if engage_live and merged.get("username") and can_perform("follow", cfg):
            try:
                res = await scripted_follow(
                    browser,
                    profile_url=merged.get("profile_url"),
                    username=merged.get("username"),
                    settings=cfg,
                )
                followed = res.ok
            except Exception as exc:
                logger.debug("scripted follow during scrape failed: %s", exc)
        merged["liked"] = liked
        merged["followed"] = followed
        enriched.append(merged)
        await asyncio.sleep(0.3 + random.uniform(0.1, 0.3))

    if not enriched and unique:
        logger.warning("Detail enrichment failed — returning %s bare URL(s)", len(unique))
        return unique[:limit]

    if not enriched:
        detail = "; ".join(errors) if errors else "no candidates from any source"
        raise ScrapeError("no_candidates", f"No posts enriched after scraping ({detail})")
    return enriched
