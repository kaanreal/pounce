"""tiny ntfy pushes for the moments that matter. silent when no topic set."""
import asyncio

import aiohttp

from .common import log

DEFAULT_NTFY_URL = "https://ntfy.sh"


def _conf(cfg):
    block = cfg.get("ntfy")
    if not isinstance(block, dict):
        return {}
    return block


def enabled(cfg):
    return bool(_conf(cfg).get("topic"))


def _base(cfg):
    url = str(_conf(cfg).get("url", DEFAULT_NTFY_URL)).rstrip("/")
    return f"{url}/"


async def send(session, cfg, title, message, tags=None):
    if not enabled(cfg):
        return
    # post to the base url with the topic inside the body. posting json to
    # /{topic} makes ntfy republish the raw blob as the message, json stays single.
    payload = {"topic": _conf(cfg)["topic"], "title": title, "message": message}
    if tags:
        payload["tags"] = tags
    try:
        async with session.post(
            _base(cfg), json=payload, timeout=aiohttp.ClientTimeout(total=8)
        ) as resp:
            if resp.status != 200:
                log().warning("ntfy push failed: http %s", resp.status)
    except asyncio.TimeoutError:
        pass
    except Exception as e:
        log().warning("ntfy push failed: %s", e)


def fire(session, cfg, title, message, tags=None):
    """fire and forget, safe from both sync and async callers"""
    if not enabled(cfg):
        return
    asyncio.ensure_future(send(session, cfg, title, message, tags))