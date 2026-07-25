"""
examples/workflows/social_empire.py

Three pre-built workflow definitions ready to POST to /workflows/run.

Usage:
    import json, httpx
    from examples.workflows.social_empire import VIRAL_PIPELINE, SHORTS_BLITZ, WARMUP_CHECK

    httpx.post("http://localhost:8000/workflows/run", json=VIRAL_PIPELINE("tech gadgets", "My Channel"))
"""
from __future__ import annotations

from typing import Any


def VIRAL_PIPELINE(
    niche: str,
    youtube_channel_title: str = "",
    subreddits: list[str] = None,
    reddit_crosspost_subs: list[str] = None,
    video_duration: int = 8,
    aspect_ratio: str = "16:9",
    privacy: str = "public",
) -> dict[str, Any]:
    """
    Full pipeline: viral research → Veo3 generate → YouTube upload
                   → Shorts repurpose → cross-post to Instagram / X / Reddit

    Context flow ({{ context.step_id.key }}):
        research    → veo3_prompt, niche, trending_topics
        generate    → path (local video file)
        yt_upload   → video_id
        yt_short    → video_id (short)
        crosspost   → results
    """
    subreddits = subreddits or []
    reddit_crosspost_subs = reddit_crosspost_subs or ["videos", "interestingvideos"]

    return {
        "workflow_id": "viral_pipeline",
        "initial_context": {
            "niche": niche,
            "channel_title": youtube_channel_title,
        },
        "steps": [
            # 1. Research the niche
            {
                "id": "research",
                "action": "social.research.viral",
                "params": {
                    "niche": "{{ context.niche }}",
                    "subreddits": subreddits,
                    "yt_results": 20,
                },
                "retry_max": 2,
                "timeout_seconds": 60,
            },
            # 2. Generate video with Veo3
            {
                "id": "generate",
                "action": "social.veo3.generate",
                "depends_on": ["research"],
                "params": {
                    "prompt": "{{ context.research.veo3_prompt }}",
                    "output_filename": "",   # engine uses uuid-based path
                    "duration_seconds": video_duration,
                    "aspect_ratio": aspect_ratio,
                },
                "retry_max": 1,
                "timeout_seconds": 300,
            },
            # 3. Upload to YouTube (full video)
            {
                "id": "yt_upload",
                "action": "social.youtube.upload",
                "depends_on": ["generate"],
                "params": {
                    "file_path": "{{ context.generate.path }}",
                    "title": "{{ context.research.trending_topics }}",
                    "description": "{{ context.niche }} — trending content",
                    "tags": [],
                    "privacy": privacy,
                    "make_short": False,
                },
                "retry_max": 2,
                "timeout_seconds": 600,
            },
            # 4. Repurpose as YouTube Short
            {
                "id": "yt_short",
                "action": "social.youtube.upload",
                "depends_on": ["generate"],
                "params": {
                    "file_path": "{{ context.generate.path }}",
                    "title": "#Shorts {{ context.research.trending_topics }}",
                    "description": "#Shorts #{{ context.niche }}",
                    "tags": ["Shorts"],
                    "privacy": privacy,
                    "make_short": True,
                },
                "retry_max": 2,
                "timeout_seconds": 300,
            },
            # 5. Cross-post to Reddit / X / Instagram
            {
                "id": "crosspost",
                "action": "social.crosspost",
                "depends_on": ["yt_upload"],
                "params": {
                    "video_url": "https://youtu.be/{{ context.yt_upload.video_id }}",
                    "title": "{{ context.research.trending_topics }}",
                    "description": "{{ context.niche }} — check this out",
                    "platforms": ["reddit", "x", "instagram"],
                    "subreddits": reddit_crosspost_subs,
                },
                "retry_max": 1,
                "timeout_seconds": 60,
            },
        ],
    }


def SHORTS_BLITZ(
    niche: str,
    count: int = 3,
    privacy: str = "public",
) -> dict[str, Any]:
    """
    Rapid 3-video Shorts generation and upload cycle.
    Each video is independently researched and generated.
    """
    steps = []

    # Single shared research step
    steps.append({
        "id": "research",
        "action": "social.research.viral",
        "params": {"niche": niche, "yt_results": 15},
        "retry_max": 2,
        "timeout_seconds": 60,
    })

    for i in range(count):
        gen_id = f"generate_{i}"
        upload_id = f"upload_{i}"

        steps.append({
            "id": gen_id,
            "action": "social.veo3.generate",
            "depends_on": ["research"],
            "params": {
                "prompt": "{{ context.research.veo3_prompt }}",
                "duration_seconds": 30,   # Shorts are ≤60s
                "aspect_ratio": "9:16",   # vertical for Shorts
            },
            "retry_max": 1,
            "timeout_seconds": 240,
        })

        steps.append({
            "id": upload_id,
            "action": "social.youtube.upload",
            "depends_on": [gen_id],
            "params": {
                "file_path": "{{ context." + gen_id + ".path }}",
                "title": f"#Shorts {{{{ context.research.trending_topics }}}} #{i + 1}",
                "description": "#Shorts",
                "tags": ["Shorts"],
                "privacy": privacy,
                "make_short": True,
            },
            "retry_max": 2,
            "timeout_seconds": 300,
        })

    return {
        "workflow_id": "shorts_blitz",
        "initial_context": {"niche": niche},
        "steps": steps,
    }


def WARMUP_CHECK(
    platforms: list[str] = None,
) -> dict[str, Any]:
    """
    Account warm-up checker — verify login health across all platforms.
    Uses the existing auth profiles via noVNC sessions.
    Useful to run before a VIRAL_PIPELINE to ensure all accounts are active.
    """
    platforms = platforms or ["youtube", "instagram", "reddit", "x"]

    steps = []
    for platform in platforms:
        steps.append({
            "id": f"check_{platform}",
            "action": "social.auth.verify",
            "params": {
                "platform": platform,
                "auth_profile": f"{platform}-default",
            },
            "retry_max": 1,
            "timeout_seconds": 30,
        })

    return {
        "workflow_id": "warmup_check",
        "initial_context": {"platforms": platforms},
        "steps": steps,
    }


# ---------------------------------------------------------------------------
# Example: run a full pipeline via the REST API
# ---------------------------------------------------------------------------

EXAMPLE_CURL = """
# 1. Run the full viral pipeline for the "AI gadgets" niche
curl -s http://localhost:8000/workflows/run \\
  -X POST -H 'content-type: application/json' \\
  -d '{workflow_json}' | jq

# 2. Monitor the run
curl -s http://localhost:8000/workflows/runs | jq '.runs[0]'

# 3. Check the dashboard
open http://localhost:8000/dashboard
"""
