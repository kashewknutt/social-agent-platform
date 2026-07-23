"""Instagram data ingestion via browser-use and Kimi."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from ig_agent.config import RAW_DIR, Settings, get_settings
from ig_agent.llm import get_browser_llm
from ig_agent.posts import (
    extract_ig_urls,
    is_agent_noise,
    merge_posts,
    normalize_posts,
)
from ig_agent.safety import (
    async_sleep,
    can_perform,
    can_start_scroll_session,
    record_action,
    record_scroll_session,
    scroll_delay,
)

logger = logging.getLogger("ig_agent.ingest")

ProgressFn = Callable[[str], None]
StopFn = Callable[[], bool]
PostsFn = Callable[[list[dict[str, Any]]], None]


def _build_ingest_task(
    settings: Settings,
    hashtags: list[str] | None = None,
    *,
    engage_live: bool = True,
) -> str:
    from ig_agent.safety import remaining_cap

    tags = hashtags or []
    tag_clause = ""
    if tags:
        tag_clause = f" Prefer hashtags: {', '.join(tags)}."

    n = settings.max_posts_per_session
    target = max(1, n)
    likes_left = remaining_cap("like", settings)
    follows_left = remaining_cap("follow", settings)

    if engage_live:
        engage_block = (
            "4) On EACH reel/post you open (fast):\n"
            "   - Do NOT click Like or Follow yourself — scripted automation handles post likes "
            "and follows on the current screen.\n"
            "   - Scroll to the next reel/post quickly. Never sit on one post.\n"
            f"   - Soft room left: likes≈{likes_left}, follows≈{follows_left}.\n"
            "   - No comments, DMs, saves, or new posts.\n"
        )
    else:
        engage_block = "4) Observation only — do not like/follow/comment/DM.\n"

    return (
        "SPEED RUN — Instagram research for Valnee Solutions (valnee.com).\n"
        "If a login wall appears, stop and say login is required.\n\n"
        "Rules:\n"
        "- Move FAST. Max ~8–12 seconds per post. Like → note URL/caption → NEXT.\n"
        "- Never linger on a post you already liked. Never loop the same URL.\n"
        "- Do not explore suggestions, stories, or Related.\n\n"
        "1) Open https://www.instagram.com/explore/ (or a hashtag search).\n"
        f"2) Open up to {n} founder/MVP/startup/SaaS/build-in-public reels.{tag_clause}\n"
        "3) Capture: post_url, caption (short), username, liked, followed.\n"
        f"{engage_block}"
        f"5) As soon as you have {target} posts with URLs, call done and return JSON only:\n"
        '{"posts":[{"post_url":"...","caption":"...","username":"...","liked":true,"followed":false,'
        '"post_type":"reel","likes":null,"views":null,"comments_count":null}]}\n'
        "Stop immediately after the JSON — do not keep scrolling."
    )


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "liked", "followed"}
    return False


async def _prompt_and_post_ingest_comment(
    browser: Any,
    post: dict[str, Any],
    *,
    controller: Any | None = None,
    run_id: str | None = None,
    settings: Settings | None = None,
    on_progress: ProgressFn | None = None,
    should_stop: StopFn | None = None,
) -> dict[str, Any]:
    from ig_agent.ingest_comment_gate import prompt_and_post_ingest_comment

    return await prompt_and_post_ingest_comment(
        browser,
        post,
        controller=controller,
        run_id=run_id,
        settings=settings,
        on_progress=on_progress,
        should_stop=should_stop,
    )


def record_live_engagements(
    posts: list[dict[str, Any]],
    *,
    run_id: str | None,
    settings: Settings | None = None,
) -> dict[str, int]:
    """Persist like/follow actions performed during browse ingest."""
    from ig_agent.persist import create_interaction, extract_post_identity, list_interactions

    cfg = settings or get_settings()
    liked_n = 0
    followed_n = 0
    existing = list_interactions(run_id=None, limit=1000)
    done_likes = {
        (r.get("post_url") or "")
        for r in existing
        if r.get("kind") == "like" and r.get("status") == "done" and r.get("post_url")
    }
    done_follows = {
        (r.get("username") or "").lower()
        for r in existing
        if r.get("kind") == "follow" and r.get("status") == "done" and r.get("username")
    }

    for post in posts:
        identity = extract_post_identity(post)
        post_url = identity.get("post_url") or post.get("post_url")
        username = identity.get("username") or post.get("username")
        profile_url = identity.get("profile_url") or (
            f"https://www.instagram.com/{username}/" if username else None
        )

        if _truthy(post.get("liked")) and post_url and post_url not in done_likes:
            if can_perform("like", cfg):
                create_interaction(
                    kind="like",
                    status="done",
                    run_id=run_id,
                    post_url=post_url,
                    profile_url=profile_url,
                    username=username,
                    auto=True,
                    payload={"source": "ingest_live", "caption": (post.get("caption") or "")[:120]},
                )
                record_action("like")
                done_likes.add(post_url)
                liked_n += 1

        if _truthy(post.get("followed")) and username and username.lower() not in done_follows:
            if can_perform("follow", cfg):
                create_interaction(
                    kind="follow",
                    status="done",
                    run_id=run_id,
                    post_url=post_url,
                    profile_url=profile_url,
                    username=username,
                    auto=True,
                    payload={"source": "ingest_live"},
                )
                record_action("follow")
                done_follows.add(username.lower())
                followed_n += 1

    return {"liked": liked_n, "followed": followed_n}


def _parse_agent_result(result: Any) -> list[dict[str, Any]]:
    """Best-effort parse of browser agent output into real Instagram posts."""
    if result is None:
        return []
    if isinstance(result, list):
        return normalize_posts([p for p in result if isinstance(p, dict)])
    if isinstance(result, dict):
        if isinstance(result.get("posts"), list):
            return normalize_posts([p for p in result["posts"] if isinstance(p, dict)])
        return normalize_posts([result])

    text = str(result)
    if not text.strip() or text.strip() in {"None", "null"}:
        return []
    # Never promote AgentOutput / thought dumps into captions
    if is_agent_noise(text):
        urls = extract_ig_urls(text)
        return normalize_posts([{"post_url": u} for u in urls]) if urls else []

    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    candidates = []
    if fence:
        candidates.append(fence.group(1).strip())
    candidates.append(text)

    found: list[dict[str, Any]] = []
    for candidate in candidates:
        for match in re.finditer(r"\{", candidate):
            chunk = candidate[match.start() :]
            try:
                data, _ = json.JSONDecoder().raw_decode(chunk)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and isinstance(data.get("posts"), list) and data["posts"]:
                found.extend(p for p in data["posts"] if isinstance(p, dict))
            elif isinstance(data, list) and data and all(isinstance(p, dict) for p in data):
                found.extend(data)

    posts = normalize_posts(found)
    if posts:
        return posts

    # Last resort: only concrete /p/ or /reel/ URLs — never dump thinking as caption
    urls = extract_ig_urls(text)
    return normalize_posts([{"post_url": u} for u in urls])


def _iter_history_items(items: Any) -> list[Any]:
    if items is None:
        return []
    if isinstance(items, (list, tuple)):
        return list(items)
    return [items]


def _posts_from_history_url_sweep(history: Any) -> list[dict[str, Any]]:
    """Pull /p/ and /reel/ URLs from any text dumped in agent history."""
    if history is None:
        return []
    chunks: list[str] = []
    for getter in (
        "model_outputs",
        "model_thoughts",
        "action_results",
        "urls",
        "visited_urls",
        "navigation_urls",
    ):
        if not hasattr(history, getter):
            continue
        try:
            val = getattr(history, getter)()
            if val is None:
                continue
            if isinstance(val, (list, tuple)):
                for item in val:
                    chunks.append(str(item))
            else:
                chunks.append(str(val))
        except Exception:
            continue
    chunks.append(str(history))
    seen: set[str] = set()
    posts: list[dict[str, Any]] = []
    for chunk in chunks:
        for url in extract_ig_urls(chunk):
            if url in seen:
                continue
            seen.add(url)
            posts.append({"post_url": url, "caption": ""})
    return normalize_posts(posts)


def _posts_from_history(history: Any) -> list[dict[str, Any]]:
    """Pull posts from a browser-use history object (including partial runs)."""
    if history is None:
        return []

    batches: list[list[dict[str, Any]]] = []

    # Structured output / final result first
    for getter in ("final_result", "extracted_content"):
        if hasattr(history, getter):
            try:
                value = getattr(history, getter)()
            except Exception:
                value = None
            posts = _parse_agent_result(value)
            if posts:
                batches.append(posts)

    # Scan each history item separately (joining dumps AgentOutput noise)
    for getter in ("model_outputs", "model_thoughts", "action_results"):
        if not hasattr(history, getter):
            continue
        try:
            items = getattr(history, getter)()
        except Exception:
            continue
        for item in _iter_history_items(items):
            posts = _parse_agent_result(item)
            if posts:
                batches.append(posts)

    if batches:
        return merge_posts(*batches)
    url_sweep = _posts_from_history_url_sweep(history)
    if url_sweep:
        return url_sweep
    return _parse_agent_result(history)


async def capture_trends(
    settings: Settings | None = None,
    hashtags: list[str] | None = None,
    *,
    on_progress: ProgressFn | None = None,
    should_stop: StopFn | None = None,
    on_posts: PostsFn | None = None,
    engage_live: bool = True,
    run_id: str | None = None,
    controller: Any | None = None,
) -> Path:
    """Run ingestion — scripted scraper first, LLM agent as fallback."""
    cfg = settings or get_settings()
    if cfg.use_scripted_scraper:
        try:
            return await _capture_trends_scripted(
                cfg,
                hashtags,
                on_progress=on_progress,
                should_stop=should_stop,
                on_posts=on_posts,
                engage_live=engage_live,
                run_id=run_id,
                controller=controller,
            )
        except Exception as exc:
            msg = f"Scripted scrape failed ({exc}) — falling back to LLM ingest"
            if on_progress:
                on_progress(msg)
            logger.warning(msg)

    return await _capture_trends_llm(
        cfg,
        hashtags,
        on_progress=on_progress,
        should_stop=should_stop,
        on_posts=on_posts,
        engage_live=engage_live,
        run_id=run_id,
        controller=controller,
    )


async def _capture_trends_scripted(
    cfg: Settings,
    hashtags: list[str] | None = None,
    *,
    on_progress: ProgressFn | None = None,
    should_stop: StopFn | None = None,
    on_posts: PostsFn | None = None,
    engage_live: bool = True,
    run_id: str | None = None,
    controller: Any | None = None,
) -> Path:
    from ig_agent.browser_factory import make_browser_session, safe_kill
    from ig_agent.scraper import ScrapeError, scrape_research_batch

    if not can_start_scroll_session(cfg):
        raise RuntimeError(
            f"Daily scroll session limit ({cfg.max_scroll_sessions_per_day}) reached."
        )

    def progress(msg: str) -> None:
        if on_progress:
            on_progress(msg)
        logger.info(msg)

    limit = max(1, cfg.max_posts_per_session)
    progress(f"Scripted scrape (target {limit} posts, engage={'ON' if engage_live else 'OFF'})…")

    browser = make_browser_session(cfg, headless=False)
    posts: list[dict[str, Any]] = []
    try:
        await browser.start()
        posts = await scrape_research_batch(
            browser,
            hashtags=hashtags,
            limit=limit,
            engage_live=engage_live,
            settings=cfg,
            on_progress=progress,
            controller=controller,
            run_id=run_id,
            should_stop=should_stop,
        )
        if on_posts:
            on_posts(posts)
        progress(f"Scripted scrape collected {len(posts)} post(s)")
        if hashtags:
            progress(f"Session hashtag(s): {', '.join('#' + str(h).lstrip('#') for h in hashtags)}")
        if engage_live:
            stats = record_live_engagements(posts, run_id=run_id, settings=cfg)
            progress(
                f"Live engage recorded: liked {stats['liked']} · followed {stats['followed']}"
            )
    except ScrapeError as exc:
        raise RuntimeError(f"Scripted scrape failed: {exc.detail}") from exc
    finally:
        await safe_kill(browser)

    if not posts:
        raise RuntimeError("Scripted scrape returned 0 posts")

    record_scroll_session()
    filename = f"scraped_{int(datetime.now().timestamp())}.json"
    out_path = RAW_DIR / filename
    out_path.write_text(
        json.dumps(
            {
                "timestamp": datetime.now().isoformat(),
                "post_count": len(posts),
                "engage_live": engage_live,
                "scraper": "scripted",
                "hashtags": hashtags or [],
                "posts": posts,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    progress(f"Saved raw scrape → {filename}")
    return out_path


async def _capture_trends_llm(
    cfg: Settings,
    hashtags: list[str] | None = None,
    *,
    on_progress: ProgressFn | None = None,
    should_stop: StopFn | None = None,
    on_posts: PostsFn | None = None,
    engage_live: bool = True,
    run_id: str | None = None,
    controller: Any | None = None,
) -> Path:
    """Run a single browser-use ingestion pass and save raw JSON."""
    from browser_use import Agent

    from ig_agent.browser_factory import make_browser_session, safe_kill
    from ig_agent.scraper import engage_current_post, harvest_posts_from_browser

    if not can_start_scroll_session(cfg):
        raise RuntimeError(
            f"Daily scroll session limit ({cfg.max_scroll_sessions_per_day}) reached."
        )

    def progress(msg: str) -> None:
        if on_progress:
            on_progress(msg)
        logger.info(msg)

    llm = get_browser_llm(cfg)
    browser = make_browser_session(cfg, headless=False)
    task = _build_ingest_task(cfg, hashtags, engage_live=engage_live)
    # Keep research passes short — lingering on one reel for minutes is useless.
    target_posts = max(1, cfg.max_posts_per_session)
    max_steps = 8 if engage_live else 7
    overall_timeout = 180 if engage_live else 150  # 3 / 2.5 minutes hard cap
    stuck_limit = 2  # same URL for this many steps → bail

    progress(
        f"Launching browser (max {max_steps} steps, {overall_timeout}s cap, "
        f"stop at {target_posts} posts"
        + (", live like/follow ON" if engage_live else "")
        + ")…"
    )

    step_count = {"n": 0}
    agent_ref: dict[str, Any] = {
        "agent": None,
        "history": None,
        "partial": [],
        "last_urls": [],
        "comment_prompted_urls": set(),
        "force_stop": False,
    }

    def _should_force_stop() -> bool:
        if agent_ref["force_stop"]:
            return True
        if should_stop and should_stop():
            return True
        partial = agent_ref.get("partial") or []
        if len(partial) >= target_posts:
            return True
        return False

    async def stop_callback() -> bool:
        return _should_force_stop()

    async def _engage_visible_reel(posts: list[dict[str, Any]]) -> None:
        if not engage_live or not posts:
            return
        try:
            head = await engage_current_post(browser, posts[0], settings=cfg)
            posts[0] = head
        except Exception:
            logger.debug("Live engage on visible reel failed", exc_info=True)

    async def on_step_end(agent_obj: Any) -> None:
        step_count["n"] += 1
        progress(
            f"Browser step {step_count['n']}/{max_steps} — "
            + ("browsing + engaging…" if engage_live else "observing Instagram…")
        )
        dom_posts: list[dict[str, Any]] = []
        try:
            dom_posts = await harvest_posts_from_browser(browser)
            if dom_posts:
                await _engage_visible_reel(dom_posts)
                prompted: set[str] = agent_ref["comment_prompted_urls"]
                head = dom_posts[0]
                head_url = str(head.get("post_url") or "")
                if head_url and head_url not in prompted:
                    prompted.add(head_url)
                    head = await _prompt_and_post_ingest_comment(
                        browser,
                        head,
                        controller=controller,
                        run_id=run_id,
                        settings=cfg,
                        on_progress=progress,
                        should_stop=should_stop,
                    )
                    dom_posts[0] = head
        except Exception:
            logger.debug("DOM harvest on step failed", exc_info=True)
        hist = getattr(agent_obj, "history", None) or getattr(agent_obj, "state", None)
        if hist is not None:
            agent_ref["history"] = hist
            try:
                step_posts = merge_posts(_posts_from_history(hist), dom_posts)
                merged = merge_posts(agent_ref.get("partial") or [], step_posts)
                if merged:
                    prev_n = len(agent_ref.get("partial") or [])
                    agent_ref["partial"] = merged
                    if on_posts:
                        on_posts(merged)
                    if len(merged) != prev_n:
                        liked = sum(1 for p in merged if _truthy(p.get("liked")))
                        followed = sum(1 for p in merged if _truthy(p.get("followed")))
                        progress(
                            f"Partial harvest: {len(merged)} post(s)"
                            + (f" · liked {liked} · followed {followed}" if engage_live else "")
                        )
                    else:
                        urls = ", ".join(
                            (p.get("post_url") or "")[:48] for p in merged[:3] if p.get("post_url")
                        )
                        if urls:
                            progress(f"Still tracking {len(merged)} post(s): {urls}")

                    # Detect stall: same single URL for several steps.
                    top = (merged[0].get("post_url") or "") if len(merged) == 1 else ""
                    if top:
                        recent = agent_ref["last_urls"]
                        recent.append(top)
                        if len(recent) > stuck_limit:
                            del recent[0 : len(recent) - stuck_limit]
                        if (
                            len(recent) >= stuck_limit
                            and all(u == top for u in recent)
                            and step_count["n"] >= stuck_limit
                        ):
                            progress(
                                f"Stuck on same post for {stuck_limit} steps — stopping early "
                                f"with {len(merged)} buffered"
                            )
                            agent_ref["force_stop"] = True
                            try:
                                agent_obj.stop()
                            except Exception:
                                pass
                    if len(merged) >= target_posts:
                        progress(f"Hit target ({len(merged)} posts) — stopping early")
                        agent_ref["force_stop"] = True
                        try:
                            agent_obj.stop()
                        except Exception:
                            pass
            except Exception:
                logger.exception("Partial harvest parse failed")
        elif dom_posts:
            merged = merge_posts(agent_ref.get("partial") or [], dom_posts)
            prev_n = len(agent_ref.get("partial") or [])
            agent_ref["partial"] = merged
            if on_posts:
                on_posts(merged)
            if len(merged) != prev_n:
                progress(f"DOM harvest: {len(merged)} post(s) from visible page")
        if should_stop and should_stop():
            progress("Stop requested — finishing current step…")
            agent_ref["force_stop"] = True
            try:
                agent_obj.stop()
            except Exception:
                pass

    heartbeat_stop = asyncio.Event()

    async def heartbeat() -> None:
        elapsed = 0
        while not heartbeat_stop.is_set():
            await asyncio.sleep(15)
            elapsed += 15
            if heartbeat_stop.is_set():
                break
            if _should_force_stop():
                progress("Stop requested — finishing browser session…")
                break
            progress(
                f"Still ingesting… {elapsed}s elapsed, step {step_count['n']}/{max_steps}"
                + (f", {len(agent_ref['partial'])} posts buffered" if agent_ref["partial"] else "")
            )

    hb_task = asyncio.create_task(heartbeat())
    posts: list[dict[str, Any]] = []
    try:
        agent = Agent(
            task=task,
            llm=llm,
            browser=browser,
            flash_mode=True,
            use_vision=False,
            use_thinking=False,
            enable_planning=False,
            use_judge=False,
            max_actions_per_step=4,
            step_timeout=30,
            register_should_stop_callback=stop_callback,
        )
        agent_ref["agent"] = agent
        try:
            history = await asyncio.wait_for(
                agent.run(max_steps=max_steps, on_step_end=on_step_end),
                timeout=overall_timeout,
            )
            agent_ref["history"] = history
            posts = merge_posts(agent_ref.get("partial") or [], _posts_from_history(history))
        except asyncio.TimeoutError:
            progress("Browser ingest hit time cap — recovering partial results…")
            hist = agent_ref.get("history")
            if hist is None and agent_ref.get("agent") is not None:
                hist = getattr(agent_ref["agent"], "history", None)
            posts = merge_posts(agent_ref.get("partial") or [], _posts_from_history(hist))
            if posts:
                progress(f"Recovered {len(posts)} post(s) after time cap")
            else:
                progress("Time cap with no recoverable posts")
        except InterruptedError:
            progress("Ingest stopped early — keeping partial harvest")
            hist = agent_ref.get("history")
            if hist is None and agent_ref.get("agent") is not None:
                hist = getattr(agent_ref["agent"], "history", None)
            posts = merge_posts(agent_ref.get("partial") or [], _posts_from_history(hist))
        if not posts and agent_ref.get("partial"):
            posts = list(agent_ref["partial"])
        try:
            dom_final = await harvest_posts_from_browser(browser)
            if dom_final:
                posts = merge_posts(posts, dom_final)
                progress(f"Final DOM harvest: {len(dom_final)} URL(s) from browser")
        except Exception:
            logger.debug("Final DOM harvest failed", exc_info=True)
        posts = merge_posts(posts)
        if on_posts:
            on_posts(posts)
        progress(f"Collected {len(posts)} post record(s)")
        if not posts:
            raise RuntimeError(
                "Ingest finished with 0 posts. "
                "Log into Instagram in the Chromium window if needed, then run again."
            )
        if engage_live:
            stats = record_live_engagements(posts, run_id=run_id, settings=cfg)
            progress(
                f"Live engage recorded: liked {stats['liked']} · followed {stats['followed']}"
            )
    except RuntimeError:
        raise
    except Exception as exc:
        detail = str(exc).strip() or repr(exc) or type(exc).__name__
        # Prefer partial harvest over hard-fail when we already scraped something
        if agent_ref.get("partial"):
            posts = merge_posts(list(agent_ref["partial"]))
            progress(f"Browser error ({detail}) — kept {len(posts)} partial post(s)")
            if engage_live and posts:
                stats = record_live_engagements(posts, run_id=run_id, settings=cfg)
                progress(
                    f"Live engage recorded: liked {stats['liked']} · followed {stats['followed']}"
                )
        else:
            progress(f"Browser ingest failed: {detail}")
            logger.exception("Browser ingest failed")
            raise RuntimeError(f"Browser ingest failed: {detail}") from exc
    finally:
        heartbeat_stop.set()
        hb_task.cancel()
        try:
            await hb_task
        except asyncio.CancelledError:
            pass
        await safe_kill(browser)

    record_scroll_session()
    output = {
        "timestamp": datetime.now().isoformat(),
        "post_count": len(posts),
        "engage_live": engage_live,
        "posts": posts,
    }
    filename = f"scraped_{int(datetime.now().timestamp())}.json"
    out_path = RAW_DIR / filename
    out_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    progress(f"Saved raw scrape → {filename}")
    return out_path


async def capture_trends_with_delays(
    settings: Settings | None = None,
    hashtags: list[str] | None = None,
    *,
    on_progress: ProgressFn | None = None,
    should_stop: StopFn | None = None,
    on_posts: PostsFn | None = None,
    engage_live: bool = True,
    run_id: str | None = None,
    controller: Any | None = None,
) -> Path:
    """Ingest with a short warm-up before browser launch."""
    if on_progress:
        on_progress("Warming up before browser launch…")
    await async_sleep(0.4, 1.0)
    path = await capture_trends(
        settings,
        hashtags,
        on_progress=on_progress,
        should_stop=should_stop,
        on_posts=on_posts,
        engage_live=engage_live,
        run_id=run_id,
        controller=controller,
    )
    await asyncio.sleep(min(scroll_delay(), 2.5))
    return path
