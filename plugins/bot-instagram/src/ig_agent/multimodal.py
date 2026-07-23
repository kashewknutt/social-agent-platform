"""Optional multimodal Reel/image analysis via Kimi Files API."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

from openai import OpenAI

from ig_agent.config import MEDIA_DIR, Settings, get_settings


def _get_client(settings: Settings) -> OpenAI:
    return OpenAI(
        api_key=settings.moonshot_api_key,
        base_url=settings.kimi_base_url,
    )


def upload_video(client: OpenAI, video_path: Path) -> str:
    """Upload a video file and return the file ID."""
    with video_path.open("rb") as f:
        uploaded = client.files.create(file=f, purpose="video")
    return uploaded.id


def analyze_image(
    image_path: Path,
    prompt: str,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Analyze a local image with Kimi vision."""
    cfg = settings or get_settings()
    client = _get_client(cfg)
    image_bytes = image_path.read_bytes()
    b64 = base64.b64encode(image_bytes).decode("ascii")
    ext = image_path.suffix.lstrip(".").lower() or "png"
    mime = f"image/{ext}" if ext != "jpg" else "image/jpeg"

    completion = client.chat.completions.create(
        model=cfg.kimi_filter_model,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"},
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    )
    content = completion.choices[0].message.content or ""
    return {"image_path": str(image_path), "analysis": content}


def analyze_video(
    video_path: Path,
    prompt: str,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Analyze a local video with Kimi vision via file upload."""
    cfg = settings or get_settings()
    client = _get_client(cfg)
    file_id = upload_video(client, video_path)

    try:
        completion = client.chat.completions.create(
            model=cfg.kimi_synth_model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "video_url",
                            "video_url": {"url": f"ms://{file_id}"},
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        )
        content = completion.choices[0].message.content or ""
    finally:
        try:
            client.files.delete(file_id)
        except Exception:
            pass

    return {"video_path": str(video_path), "analysis": content}


def analyze_top_posts(
    filtered_posts: list[dict[str, Any]],
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    """Run multimodal analysis on top-N filtered posts with local media."""
    cfg = settings or get_settings()
    if not cfg.enable_multimodal:
        return []

    top_n = cfg.multimodal_top_n
    sorted_posts = sorted(
        filtered_posts,
        key=lambda p: p.get("relevance_score", 0),
        reverse=True,
    )[:top_n]

    prompt = (
        "Analyze this Instagram Reel/post for: hook structure, visual style, "
        "text overlays, pacing, and how a B2B software agency could adapt it."
    )
    notes: list[dict[str, Any]] = []

    for post in sorted_posts:
        media_path = post.get("media_path") or post.get("screenshot_path")
        if not media_path:
            continue
        path = Path(media_path)
        if not path.is_absolute():
            path = MEDIA_DIR / path.name
        if not path.exists():
            continue

        suffix = path.suffix.lower()
        if suffix in (".mp4", ".mov", ".webm", ".avi"):
            note = analyze_video(path, prompt, cfg)
        elif suffix in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            note = analyze_image(path, prompt, cfg)
        else:
            continue

        note["post_url"] = post.get("post_url")
        note["relevance_score"] = post.get("relevance_score")
        notes.append(note)

    return notes


def analyze_from_filtered_file(
    filtered_path: Path,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    """Load filtered JSON and run multimodal on top posts."""
    data = json.loads(filtered_path.read_text(encoding="utf-8"))
    return analyze_top_posts(data.get("posts", []), settings)


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Best-effort extraction of a JSON object from model text (handles ```json fences)."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:\w+)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _analyzer_system_prompt(agency: dict[str, Any], context_note: str | None) -> str:
    """Prompt for turning the user's own video into a ready-to-post caption.

    This is OUR post going out under the brand's own voice (not a comment or
    DM on someone else's content), so it borrows the same brand-facts / locale
    / voice-guardrail rules used for organic post drafts and comment/DM copy
    elsewhere in this codebase, to keep tone (and the "don't sound like AI"
    fixes) consistent app-wide.
    """
    from ig_agent.propose import (
        _brand_facts,
        _locale_voice_block,
        _trending_hashtag_pool,
        _voice_guardrails_block,
        detect_locale,
    )

    locale = detect_locale({}, agency)
    context_block = ""
    if context_note and context_note.strip():
        context_block = (
            f"\nAdditional context from the creator about this video (use it, don't quote "
            f"it verbatim):\n{context_note.strip()[:500]}\n"
        )
    return (
        "Watch this video and write everything needed to post it to Instagram, in the "
        "account's own voice. This is OUR content going out, not a comment on someone "
        "else's post.\n"
        f"{_locale_voice_block(locale)}\n"
        f"{_brand_facts(agency)}\n"
        f"{_trending_hashtag_pool(agency)}"
        f"{context_block}\n"
        "Output STRICT JSON only, no markdown fences, no explanation, in this exact shape:\n"
        '{"title": "...", "caption": "...", "hashtags": ["...", "..."]}\n\n'
        "CRITICAL, read this before writing anything: if the video has someone speaking or "
        "narrating, do NOT write a caption that transcribes, summarizes, or lightly "
        "reword what they said. The audience already hears that audio when they watch. A "
        "caption that just repeats it, even 'cleaned up', is the laziest possible caption "
        "and is exactly what you must avoid. The caption's entire job is to add something "
        "the audio does NOT already say out loud: the backstory or why you made this, a "
        "number or result, a strong opinion or take, a reaction, or a call to action. If "
        "you find yourself writing something close to a line spoken in the video, stop and "
        "write a completely different angle instead.\n\n"
        "RULES:\n"
        "1. \"title\" is a short, punchy hook line (under 60 characters) that stops someone "
        "mid-scroll. It must NOT be a line of dialogue/narration from the video and must "
        "NOT describe what's visually happening on screen.\n"
        "2. \"caption\" is the full Instagram caption: hook in line 1, 3-6 short lines. "
        "Write it as what you'd type UNDER the video after uploading it, assuming everyone "
        "reading it already watched and heard it. You can reference the video's premise or "
        "outcome in your own words for context, but never restate the actual spoken lines "
        "or narrate what's on screen shot by shot. Write it the way a real creator would "
        "type a caption on their phone. Soft brand close is fine but no strategy dump.\n"
        "3. \"hashtags\" is a list of EXACTLY 4-5 lowercase hashtags (no '#' prefix). "
        "Pick ones that are currently active/trending for this specific niche on "
        "Instagram right now, the kind an actual creator in this space would use today. "
        "No generic filler (love, instagood, motivation, viral) and no obscure tags "
        "nobody searches. Fewer, sharper tags beat a long list.\n"
        f"4. {_voice_guardrails_block()}\n"
        "5. Do not mention being an AI or reference this prompt."
    )


def analyze_video_for_caption(
    video_path: Path,
    *,
    context_note: str | None = None,
    agency: dict[str, Any] | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Analyze a local video and draft a title/caption/hashtags for posting it.

    Reuses the same Kimi Files API upload flow as `analyze_video` (one call,
    same model/cleanup), just with a copywriting prompt instead of a plain
    description prompt, and JSON parsing of the result.
    """
    cfg = settings or get_settings()
    ctx = agency or {}
    prompt = _analyzer_system_prompt(ctx, context_note)

    client = _get_client(cfg)
    file_id = upload_video(client, video_path)
    try:
        completion = client.chat.completions.create(
            model=cfg.kimi_synth_model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "video_url", "video_url": {"url": f"ms://{file_id}"}},
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        )
        raw_text = completion.choices[0].message.content or ""
    finally:
        try:
            client.files.delete(file_id)
        except Exception:
            pass

    from ig_agent.propose import strip_ai_tell_symbols

    parsed = _extract_json_object(raw_text)
    if parsed:
        title = strip_ai_tell_symbols(str(parsed.get("title") or "").strip()).strip()
        caption = strip_ai_tell_symbols(str(parsed.get("caption") or "").strip()).strip()
        hashtags_raw = parsed.get("hashtags")
        hashtags = (
            [str(h).strip().lstrip("#").lower() for h in hashtags_raw if str(h).strip()]
            if isinstance(hashtags_raw, list)
            else []
        )
        # Hard cap even if the model ignores the "4-5" instruction.
        hashtags = hashtags[:5]
    else:
        # Degrade gracefully: keep the raw text as caption so the user can
        # still edit/fill title + hashtags by hand instead of a hard failure.
        title, caption, hashtags = "", strip_ai_tell_symbols(raw_text.strip()).strip(), []

    return {
        "title": title,
        "caption": caption,
        "hashtags": hashtags,
        "raw_analysis": raw_text,
    }
