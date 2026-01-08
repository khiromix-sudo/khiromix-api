import asyncio
import os
import re
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, Body
from fastapi.responses import JSONResponse
import yt_dlp

app = FastAPI(title="KHIRO Simple Extract API", version="1.0.0")


def _err(msg: str, code: int = 400):
    return JSONResponse(status_code=code, content={"ok": False, "error": msg})


def _is_direct_media(url: str) -> bool:
    u = (url or "").lower().split("?")[0]
    return any(u.endswith(x) for x in [".mp4", ".m3u8", ".mpd"])


def _sort_key(q: str) -> Tuple[int, int]:
    m = re.search(r"(\d{3,4})p", q or "")
    if m:
        return (0, -int(m.group(1)))
    return (1, 0)


@dataclass
class PickItem:
    quality: str
    url: str


def _has_audio_or_manifest(f: dict) -> bool:
    acodec = f.get("acodec")
    if acodec and acodec != "none":
        return True
    ext = (f.get("ext") or "").lower()
    if ext in ["m3u8", "mpd"]:
        return True
    proto = (f.get("protocol") or "").lower()
    if "m3u8" in proto or "dash" in proto:
        return True
    return False


def _select_items(info: dict) -> List[PickItem]:
    formats = info.get("formats") or []
    best_by_height: Dict[int, str] = {}
    fallback_by_height: Dict[int, str] = {}

    for f in formats:
        if not isinstance(f, dict):
            continue
        url = f.get("url")
        if not url:
            continue

        vcodec = f.get("vcodec")
        if vcodec == "none":
            continue

        h = f.get("height")
        if not isinstance(h, int) or h <= 0:
            continue

        if _has_audio_or_manifest(f):
            if h not in best_by_height:
                best_by_height[h] = url
        else:
            if h not in fallback_by_height:
                fallback_by_height[h] = url

    src = best_by_height if best_by_height else fallback_by_height
    items = [PickItem(quality=f"{h}p", url=u) for h, u in src.items()]
    items.sort(key=lambda it: _sort_key(it.quality))
    return items


def _ydl_extract(url: str, headers: Optional[Dict[str, str]], cookiefile: Optional[str]) -> dict:
    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "extract_flat": False,
    }
    if headers:
        opts["http_headers"] = headers
    if cookiefile:
        opts["cookiefile"] = cookiefile

    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


async def _run_extract(url: str, headers: Optional[Dict[str, str]], cookiefile: Optional[str]) -> dict:
    return await asyncio.to_thread(_ydl_extract, url, headers, cookiefile)


@app.post("/extract")
async def extract(payload: Dict[str, Any] = Body(...)):
    app_name = (payload.get("app_name") or "").strip()
    name = (payload.get("name") or "").strip()
    url = (payload.get("url") or "").strip()

    if not app_name:
        return _err("app_name is required")
    if not name:
        return _err("name is required")
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        return _err("url is required and must start with http/https")

    headers = payload.get("headers")
    if headers is not None and not isinstance(headers, dict):
        return _err("headers must be an object")

    cookies = payload.get("cookies")
    if cookies is not None and not isinstance(cookies, str):
        return _err("cookies must be a string")

    is_facebook = ".facebook.com" in (url or "").lower()

    cookie_path = None
    try:
        if is_facebook and cookies and cookies.strip():
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
            tmp.write(cookies.encode("utf-8", errors="ignore"))
            tmp.close()
            cookie_path = tmp.name

        info = await _run_extract(url, headers=headers, cookiefile=cookie_path if is_facebook else None)
        items = _select_items(info)

        if not items and _is_direct_media(url):
            items = [PickItem(quality="direct", url=url)]

        if not items:
            return _err("failed to extract formats", 422)

        out = [{"quality": it.quality, "url": it.url, "name": name} for it in items]
        return JSONResponse(content=out)

    except Exception as e:
        return _err(str(e), 500)

    finally:
        if cookie_path:
            try:
                os.unlink(cookie_path)
            except Exception:
                pass
