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


def _quality_from_resolution(res: Optional[str], width: Optional[int], height: Optional[int]) -> Optional[str]:
    w = None
    h = None
    if isinstance(res, str) and "x" in res:
        a, b = res.lower().split("x", 1)
        try:
            w = int(a.strip())
            h = int(b.strip())
        except Exception:
            w = None
            h = None
    if isinstance(width, int) and width > 0:
        w = width
    if isinstance(height, int) and height > 0:
        h = height
    if isinstance(w, int) and w > 0:
        if w >= 1920:
            return "1080p"
        if w >= 1280:
            return "720p"
        if w >= 854:
            return "480p"
        if w >= 640:
            return "360p"
        if w >= 426:
            return "240p"
        return "144p"
    if isinstance(h, int) and h > 0:
        if h >= 1080:
            return "1080p"
        if h >= 720:
            return "720p"
        if h >= 480:
            return "480p"
        if h >= 360:
            return "360p"
        if h >= 240:
            return "240p"
        return "144p"
    return None


def _is_m3u8_format(f: dict) -> bool:
    ext = (f.get("ext") or "").lower()
    if ext == "m3u8":
        return True
    proto = (f.get("protocol") or "").lower()
    if "m3u8" in proto:
        return True
    url = (f.get("url") or "").lower()
    if ".m3u8" in url:
        return True
    return False


def _has_av_in_one(f: dict) -> bool:
    vcodec = f.get("vcodec")
    acodec = f.get("acodec")
    return (vcodec and vcodec != "none") and (acodec and acodec != "none")


def _select_items(info: dict) -> List[PickItem]:
    formats = info.get("formats") or []
    best_by_quality: Dict[str, str] = {}
    m3u8_by_quality: Dict[str, str] = {}
    fallback_by_quality: Dict[str, str] = {}

    for f in formats:
        if not isinstance(f, dict):
            continue
        url = f.get("url")
        if not url:
            continue

        q = _quality_from_resolution(f.get("resolution"), f.get("width"), f.get("height"))
        if not q:
            continue

        if _has_av_in_one(f):
            if q not in best_by_quality:
                best_by_quality[q] = url
            continue

        if _is_m3u8_format(f):
            if q not in m3u8_by_quality:
                m3u8_by_quality[q] = url
            continue

        if q not in fallback_by_quality:
            fallback_by_quality[q] = url

    merged: Dict[str, str] = {}
    for q in set(list(best_by_quality.keys()) + list(m3u8_by_quality.keys()) + list(fallback_by_quality.keys())):
        if q in best_by_quality:
            merged[q] = best_by_quality[q]
        elif q in m3u8_by_quality:
            merged[q] = m3u8_by_quality[q]
        else:
            merged[q] = fallback_by_quality[q]

    items = [PickItem(quality=q, url=u) for q, u in merged.items()]
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
