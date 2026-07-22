"""Fast, selector-based Instagram actions (no LLM on the happy path)."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from ig_agent.config import DATA_DIR, Settings, get_settings

logger = logging.getLogger("ig_agent.scripted_actions")

FALLBACK_LOG = DATA_DIR / "scripted_fallback_log.json"

Reason = Literal[
    "login_wall",
    "checkpoint",
    "blocked",
    "selector_not_found",
    "verify_failed",
    "navigation_failed",
]

_CHALLENGE_MARKERS = (
    "log in",
    "login",
    "sign up",
    "checkpoint",
    "challenge",
    "suspicious",
    "try again later",
    "confirm it's you",
    "confirm it is you",
    "we restrict",
    "temporarily locked",
    "action blocked",
)

_LIKE_SELECTORS = (
    'svg[aria-label="Like"]',
    'svg[aria-label="Like post"]',
    'span svg[aria-label="Like"]',
)

_UNLIKE_SELECTORS = (
    'svg[aria-label="Unlike"]',
    'svg[aria-label="Unlike post"]',
)

_COMMENT_TEXTAREA_SELECTORS = (
    'textarea[aria-label="Add a comment…"]',
    'textarea[aria-label="Add a comment..."]',
    'textarea[placeholder="Add a comment…"]',
    'textarea[placeholder="Add a comment..."]',
    "form textarea",
)

_DM_COMPOSER_SELECTORS = (
    'div[aria-label="Message"][contenteditable="true"]',
    'div[contenteditable="true"][role="textbox"]',
    'div[contenteditable="true"]',
)


@dataclass
class ActionResult:
    ok: bool
    detail: str
    already_done: bool = False


class ScriptedActionError(Exception):
    def __init__(self, reason: Reason, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail or reason
        super().__init__(f"{reason}: {self.detail}")


async def _ensure_page(browser: Any) -> Any:
    """Return an Actor Page, starting the browser session if needed."""
    if hasattr(browser, "get_current_page"):
        page = await browser.get_current_page()
        if page is not None:
            return page
    if hasattr(browser, "start"):
        await browser.start()
        page = await browser.get_current_page()
        if page is not None:
            return page
    raise ScriptedActionError("navigation_failed", "No browser page available")


async def _navigate(browser: Any, url: str, *, settle: float = 1.2) -> Any:
    page = await _ensure_page(browser)
    await browser.navigate_to(url)
    await asyncio.sleep(settle + random.uniform(0.1, 0.4))
    await _dismiss_known_dialogs(page)
    await _check_blockers(page)
    return page


async def _wait_for_css(page: Any, selector: str, timeout: float) -> Any | None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        els = await page.get_elements_by_css_selector(selector)
        if els:
            return els[0]
        await asyncio.sleep(0.2)
    return None


async def _dismiss_known_dialogs(page: Any) -> None:
    await page.evaluate(
        """() => {
  const labels = ['Not Now', 'Cancel', 'Close', 'Dismiss', 'Later'];
  for (const el of document.querySelectorAll('button, div[role="button"]')) {
    const t = (el.innerText || el.textContent || '').trim();
    if (labels.some(l => t === l || t.startsWith(l))) {
      el.click();
      return true;
    }
  }
  return false;
}"""
    )
    await asyncio.sleep(0.15)


async def _check_blockers(page: Any) -> None:
    text = await page.evaluate("() => (document.body && document.body.innerText) || ''")
    low = str(text or "").lower()
    if any(m in low for m in ("log in to instagram", "sign up to see", "log in to continue")):
        raise ScriptedActionError("login_wall", "Login wall detected")
    if "checkpoint" in low or "confirm it's you" in low or "confirm it is you" in low:
        raise ScriptedActionError("checkpoint", "Security checkpoint detected")
    if any(m in low for m in ("try again later", "action blocked", "we restrict")):
        raise ScriptedActionError("blocked", "Instagram blocked or throttled this action")


async def _find_button_by_text(page: Any, texts: tuple[str, ...]) -> bool:
    found = await page.evaluate(
        """(labels) => {
  const want = labels.map(l => l.toLowerCase());
  for (const el of document.querySelectorAll('button, div[role="button"], span')) {
    const t = (el.innerText || el.textContent || '').trim();
    if (!t) continue;
    const low = t.toLowerCase();
    if (want.some(w => low === w || low.startsWith(w))) {
      el.click();
      return true;
    }
  }
  return false;
}""",
        list(texts),
    )
    return bool(found)


async def _click_first(page: Any, selectors: tuple[str, ...], timeout: float) -> bool:
    for sel in selectors:
        el = await _wait_for_css(page, sel, timeout / max(len(selectors), 1))
        if el is not None:
            await el.click()
            await asyncio.sleep(0.25 + random.uniform(0.05, 0.2))
            return True
    return False


async def _is_liked(page: Any) -> bool:
    for sel in _UNLIKE_SELECTORS:
        els = await page.get_elements_by_css_selector(sel)
        if els:
            return True
    return False


async def _double_tap_reel(page: Any) -> bool:
    tapped = await page.evaluate(
        """() => {
  const v = document.querySelector('video');
  if (!v) return false;
  const r = v.getBoundingClientRect();
  const x = r.left + r.width / 2;
  const y = r.top + r.height / 2;
  for (const type of ['pointerdown', 'pointerup', 'click']) {
    v.dispatchEvent(new PointerEvent(type, { bubbles: true, cancelable: true, clientX: x, clientY: y }));
  }
  return true;
}"""
    )
    return str(tapped).lower() in {"true", "1"}


async def scripted_like_current(
    browser: Any,
    *,
    settings: Settings | None = None,
) -> ActionResult:
    """Like the reel/post currently on screen (no navigation away)."""
    cfg = settings or get_settings()
    timeout = cfg.scripted_action_timeout
    page = await _ensure_page(browser)
    await _dismiss_known_dialogs(page)
    if await _is_liked(page):
        return ActionResult(True, "Already liked", already_done=True)
    if await _click_first(page, _LIKE_SELECTORS, timeout):
        await asyncio.sleep(0.45)
        if await _is_liked(page):
            return ActionResult(True, "Liked current post (heart button)")
    if await _double_tap_reel(page):
        await asyncio.sleep(0.5)
        if await _is_liked(page):
            return ActionResult(True, "Liked current post (double-tap)")
    raise ScriptedActionError("selector_not_found", "Like button not found on current view")


async def scripted_like(
    browser: Any,
    post_url: str,
    *,
    settings: Settings | None = None,
) -> ActionResult:
    cfg = settings or get_settings()
    timeout = cfg.scripted_action_timeout
    page = await _navigate(browser, post_url, settle=1.0)
    if await _is_liked(page):
        return ActionResult(True, "Already liked", already_done=True)
    if not await _click_first(page, _LIKE_SELECTORS, timeout):
        raise ScriptedActionError("selector_not_found", "Like button not found")
    await asyncio.sleep(0.4)
    if not await _is_liked(page):
        raise ScriptedActionError("verify_failed", "Like did not register")
    return ActionResult(True, f"Liked {post_url}")


async def scripted_follow(
    browser: Any,
    *,
    profile_url: str | None = None,
    username: str | None = None,
    settings: Settings | None = None,
) -> ActionResult:
    cfg = settings or get_settings()
    timeout = cfg.scripted_action_timeout
    target = profile_url or (f"https://www.instagram.com/{username}/" if username else "")
    if not target:
        raise ScriptedActionError("selector_not_found", "No profile URL or username")
    page = await _navigate(browser, target, settle=1.0)
    state = await page.evaluate(
        """() => {
  for (const el of document.querySelectorAll('button,div[role="button"]')) {
    const t = (el.innerText || el.textContent || '').trim();
    if (/^(Following|Requested|Follow)$/i.test(t)) return t;
  }
  return '';
}"""
    )
    state_str = str(state or "").strip()
    if state_str.lower() in {"following", "requested"}:
        return ActionResult(True, f"Already {state_str}", already_done=True)
    if not await _find_button_by_text(page, ("Follow",)):
        raise ScriptedActionError("selector_not_found", "Follow button not found")
    await asyncio.sleep(0.5)
    after = await page.evaluate(
        """() => {
  for (const el of document.querySelectorAll('button,div[role="button"]')) {
    const t = (el.innerText || el.textContent || '').trim();
    if (/^(Following|Requested)$/i.test(t)) return t;
  }
  return '';
}"""
    )
    if not str(after or "").strip():
        raise ScriptedActionError("verify_failed", "Follow did not register")
    return ActionResult(True, f"Followed {target} ({after})")


async def scripted_comment_current(
    browser: Any,
    text: str,
    *,
    settings: Settings | None = None,
) -> ActionResult:
    """Post a comment on the reel/post currently on screen."""
    cfg = settings or get_settings()
    timeout = cfg.scripted_action_timeout
    comment = (text or "").strip()
    if not comment:
        raise ScriptedActionError("verify_failed", "Empty comment text")
    page = await _ensure_page(browser)
    await _dismiss_known_dialogs(page)
    el = None
    for sel in _COMMENT_TEXTAREA_SELECTORS:
        el = await _wait_for_css(page, sel, timeout / max(len(_COMMENT_TEXTAREA_SELECTORS), 1))
        if el is not None:
            break
    if el is None:
        raise ScriptedActionError("selector_not_found", "Comment box not found on current view")
    await el.click()
    await el.fill(comment, clear=True)
    await asyncio.sleep(0.3)
    if not await _find_button_by_text(page, ("Post",)):
        raise ScriptedActionError("selector_not_found", "Post button not found")
    await asyncio.sleep(0.8)
    snippet = comment[:80]
    visible = await page.evaluate(
        """(snippet) => {
  const body = document.body ? document.body.innerText : '';
  return body.includes(snippet);
}""",
        snippet,
    )
    if not str(visible).lower() in {"true", "1"}:
        raise ScriptedActionError("verify_failed", "Comment not visible after posting")
    return ActionResult(True, f"Comment posted: {snippet}")


async def scripted_comment(
    browser: Any,
    post_url: str,
    text: str,
    *,
    settings: Settings | None = None,
) -> ActionResult:
    cfg = settings or get_settings()
    timeout = cfg.scripted_action_timeout
    comment = (text or "").strip()
    if not comment:
        raise ScriptedActionError("verify_failed", "Empty comment text")
    page = await _navigate(browser, post_url, settle=1.2)
    el = None
    for sel in _COMMENT_TEXTAREA_SELECTORS:
        el = await _wait_for_css(page, sel, timeout / len(_COMMENT_TEXTAREA_SELECTORS))
        if el is not None:
            break
    if el is None:
        raise ScriptedActionError("selector_not_found", "Comment box not found")
    await el.click()
    await el.fill(comment, clear=True)
    await asyncio.sleep(0.3)
    if not await _find_button_by_text(page, ("Post",)):
        raise ScriptedActionError("selector_not_found", "Post button not found")
    await asyncio.sleep(0.8)
    snippet = comment[:80]
    visible = await page.evaluate(
        """(snippet) => {
  const body = document.body ? document.body.innerText : '';
  return body.includes(snippet);
}""",
        snippet,
    )
    if not visible:
        raise ScriptedActionError("verify_failed", "Comment not visible after posting")
    return ActionResult(True, f"Comment posted on {post_url}: {snippet}")


async def scripted_dm(
    browser: Any,
    text: str,
    *,
    profile_url: str | None = None,
    username: str | None = None,
    settings: Settings | None = None,
) -> ActionResult:
    cfg = settings or get_settings()
    timeout = cfg.scripted_action_timeout
    message = (text or "").strip()
    if not message:
        raise ScriptedActionError("verify_failed", "Empty DM text")
    target = profile_url or (f"https://www.instagram.com/{username}/" if username else "")
    if not target:
        raise ScriptedActionError("selector_not_found", "No profile URL or username")
    page = await _navigate(browser, target, settle=1.0)
    if not await _find_button_by_text(page, ("Message", "Send message")):
        raise ScriptedActionError("selector_not_found", "Message button not found")
    await asyncio.sleep(0.8)
    await _dismiss_known_dialogs(page)
    composer = None
    for sel in _DM_COMPOSER_SELECTORS:
        composer = await _wait_for_css(page, sel, timeout / len(_DM_COMPOSER_SELECTORS))
        if composer is not None:
            break
    if composer is None:
        raise ScriptedActionError("selector_not_found", "DM composer not found")
    await composer.click()
    # contenteditable — fill via JS
    await composer.evaluate(
        """(msg) => {
  this.focus();
  this.textContent = msg;
  this.dispatchEvent(new InputEvent('input', { bubbles: true }));
}""",
        message,
    )
    await asyncio.sleep(0.3)
    if not await _find_button_by_text(page, ("Send",)):
        await page.press("Enter")
    await asyncio.sleep(0.8)
    snippet = message[:60]
    visible = await page.evaluate(
        """(snippet) => {
  const body = document.body ? document.body.innerText : '';
  return body.includes(snippet);
}""",
        snippet,
    )
    if not visible:
        raise ScriptedActionError("verify_failed", "DM not visible after send")
    who = username or target
    return ActionResult(True, f"DM sent to {who}: {snippet}")


def record_scripted_fallback(kind: str, reason: str) -> None:
    today = str(date.today())
    data: dict[str, Any] = {"date": today, "total": 0, "by_kind": {}, "events": []}
    if FALLBACK_LOG.exists():
        try:
            raw = json.loads(FALLBACK_LOG.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("date") == today:
                data = raw
        except Exception:
            pass
    data["total"] = int(data.get("total") or 0) + 1
    by_kind = dict(data.get("by_kind") or {})
    by_kind[kind] = int(by_kind.get(kind) or 0) + 1
    data["by_kind"] = by_kind
    events = list(data.get("events") or [])
    events.append({"kind": kind, "reason": reason})
    data["events"] = events[-50:]
    FALLBACK_LOG.parent.mkdir(parents=True, exist_ok=True)
    FALLBACK_LOG.write_text(json.dumps(data, indent=2), encoding="utf-8")
    logger.warning("Scripted fallback: %s — %s", kind, reason)


def scripted_health_snapshot() -> dict[str, Any]:
    today = str(date.today())
    if not FALLBACK_LOG.exists():
        return {"date": today, "total": 0, "by_kind": {}, "needs_selector_update": False}
    try:
        data = json.loads(FALLBACK_LOG.read_text(encoding="utf-8"))
    except Exception:
        return {"date": today, "total": 0, "by_kind": {}, "needs_selector_update": False}
    if data.get("date") != today:
        return {"date": today, "total": 0, "by_kind": {}, "needs_selector_update": False}
    total = int(data.get("total") or 0)
    return {
        "date": today,
        "total": total,
        "by_kind": data.get("by_kind") or {},
        "recent": (data.get("events") or [])[-5:],
        "needs_selector_update": total > 0,
        "message": (
            "Some actions fell back to AI today — update selectors in scripted_actions.py"
            if total > 0
            else None
        ),
    }


FALLBACK_NOTE = (
    "[scripted automation missed a selector — fell back to AI; update scripted_actions.py]"
)
