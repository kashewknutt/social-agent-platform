"""Optional multimodal Reel/image analysis via Kimi Files API."""

from __future__ import annotations

import base64
import json
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
