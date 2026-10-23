# -*- coding: utf-8 -*-
"""
以圖搜圖畫師與標籤反查與校正引擎 (reverse_search.py)
結合：
1. Danbooru MD5 精準雜湊直查 + Danbooru Post/Artist JSON API (直接取得 100% 認證畫師、Twitter @帳號、Pixiv ID、漢字別名、角色、作品與完整標籤)
2. IQDB 多圖庫視覺比對 (自動串聯 Danbooru / Yande.re / Gelbooru API 取回結構化認證畫師與標籤，無嚴格次數限制)
3. SauceNAO (精準抓取最新 Twitter @帳號、推文網址、Pixiv 繪師、Fanbox/Fantia 作者)
4. Google Lens 以圖搜圖連結生成 + Web 搜尋交叉比對 (供 MCP Gemini Flash 3.8 進行多模態畫師比對與標籤校正)
5. 自動標籤校正 (Tag Calibration)：一旦確認真實畫師/角色/作品，自動清除先前本機 AI 低信心度猜錯的畫師與錯誤標籤
"""

import hashlib
import io
import json
import os
import re
import threading
from background_work import update_job
import time
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageFile
from background_work import (
    BackgroundBusy, acquire_job, exclusive_job, start_job_thread,
    background_checkpoint, media_io,
)

from database import (
    GALLERY_ROOT,
    MODEL_DIR,
    NON_ARTIST_FOLDERS,
    get_db,
    ensure_tag,
    refresh_tag_counts,
    log_operation,
)
from zh_dict import (
    DANBOORU_ARTIST_TO_FOLDER,
    FOLDER_TO_DANBOORU_ARTIST,
    get_tag_zh,
    resolve_artist_folder_name,
)

ImageFile.LOAD_TRUNCATED_IMAGES = True

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "zh-TW,zh;q=0.9,ja;q=0.8,en-US;q=0.7,en;q=0.6",
}
BOORU_HEADERS = {
    "User-Agent": "TwitterTagGallery/2.0 (Local Gallery Organizer; MCP Gemini Flash 3.8)",
    "Accept": "application/json",
}

RE_SIMILARITY = re.compile(r"(\d+(?:\.\d+)?)%")
RE_TWITTER_HANDLE = re.compile(
    r"(?:twitter\.com|x\.com)/([A-Za-z0-9_]{1,30})(?:/status/\d+)?", re.IGNORECASE
)
RE_PIXIV_ARTWORK = re.compile(
    r"pixiv\.net/(?:(?:[a-z]{2}/)?artworks/|member_illust\.php\?[^\"'\s]*illust_id=)(\d+)",
    re.IGNORECASE,
)
RE_PIXIV_USER = re.compile(
    r"(?:pixiv\.net/(?:[a-z]{2}/)?users/|pixiv\.net/member(?:\.php\?id=|/)|pixiv\.net/fanbox/creator/|kemono\.(?:su|cr|party)/fanbox/user/)(\d+)",
    re.IGNORECASE,
)
RE_FANBOX_CREATOR = re.compile(
    r"(?:fanbox\.cc/@([A-Za-z0-9_-]+)|https?://([A-Za-z0-9_-]+)\.fanbox\.cc)",
    re.IGNORECASE,
)
RE_FANBOX_POST = re.compile(
    r"(?:fanbox\.cc/@[A-Za-z0-9_-]+/posts/|[A-Za-z0-9_-]+\.fanbox\.cc/posts/|pixiv\.net/fanbox/creator/\d+/post/|kemono\.(?:su|cr|party)/fanbox/user/\d+/post/)(\d+)",
    re.IGNORECASE,
)
RE_FANTIA_CLUB = re.compile(r"fantia\.jp/(?:fanclubs|posts)/(\d+)", re.IGNORECASE)
RE_FILENAME_FANBOX_POST = re.compile(r"^(\d{6,9})_(\d{1,3})(?:\s*\(\d+\))?$")
RE_FILENAME_TWITTER_STATUS = re.compile(r"(?:^|[^0-9])(1[3-9]\d{17}|2[0-9]\d{17})(?:[^0-9]|$)")
RE_FILENAME_TWITTER_HANDLE_STATUS = re.compile(r"^([A-Za-z0-9_]{3,20})[-_](1[3-9]\d{17}|2[0-9]\d{17})(?:[-_].*)?$")
RE_DANBOORU_POST = re.compile(r"danbooru\.donmai\.us/posts/(\d+)", re.IGNORECASE)
RE_YANDERE_POST = re.compile(r"yande\.re/post/show/(\d+)", re.IGNORECASE)
RE_IQDB_ALT = re.compile(r"Rating:\s*([gsqe])\s+Score:\s*\S+\s+Tags:\s*(.+)$", re.IGNORECASE)
RE_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]")

FANBOX_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Origin": "https://www.fanbox.cc",
    "Referer": "https://www.fanbox.cc/",
    "Accept": "application/json",
}

RATING_MAP = {
    "g": "general",
    "s": "sensitive",
    "q": "questionable",
    "e": "explicit",
}

VERIFIED_ARTIST_SOURCES = (
    "pixiv",
    "saucenao",
    "iqdb",
    "danbooru",
    "twitter",
    "gemini_mcp",
    "folder",
    "manual",
)

ARTIST_CACHE_FILE = Path(__file__).parent / "artist_cache.json"

# Lazy-loaded tag category dictionary from Camie metadata if available
_TAG_CATEGORY_MAP: Dict[str, str] = {}
_DANBOORU_ARTIST_CACHE: Dict[str, Dict[str, Any]] = {}
_ARTIST_CACHE_LOADED: bool = False
_ARTIST_CACHE_DIRTY_COUNT: int = 0
_EXISTING_FOLDERS_CACHE: Tuple[ float, List[str] ] = (0.0, [])


def _ensure_artist_cache_loaded():
    global _ARTIST_CACHE_LOADED, _DANBOORU_ARTIST_CACHE
    if _ARTIST_CACHE_LOADED:
        return
    _ARTIST_CACHE_LOADED = True
    if ARTIST_CACHE_FILE.exists():
        try:
            with open(ARTIST_CACHE_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if isinstance(v, dict):
                        norm_name = v.get("name") or v.get("db_name") or ""
                        _DANBOORU_ARTIST_CACHE[k] = {
                            "name": norm_name,
                            "other_names": v.get("other_names") or [],
                            "cjk_name": v.get("cjk_name"),
                            "twitter_handles": v.get("twitter_handles") or [],
                            "pixiv_user_id": v.get("pixiv_user_id"),
                            "urls": v.get("urls") or [],
                            "suggested_folder": v.get("suggested_folder"),
                            "resolved": True,
                        }
        except Exception:
            pass


def save_persistent_artist_cache(force: bool = False):
    global _ARTIST_CACHE_DIRTY_COUNT
    if not force and _ARTIST_CACHE_DIRTY_COUNT < 25:
        return
    try:
        _ensure_artist_cache_loaded()
        existing = {}
        if ARTIST_CACHE_FILE.exists():
            try:
                with open(ARTIST_CACHE_FILE, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception:
                existing = {}
        existing.update(_DANBOORU_ARTIST_CACHE)
        tmp_file = ARTIST_CACHE_FILE.with_suffix(".tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        tmp_file.replace(ARTIST_CACHE_FILE)
        _ARTIST_CACHE_DIRTY_COUNT = 0
    except Exception:
        pass


def get_known_tag_category(tag_name: str) -> str:
    global _TAG_CATEGORY_MAP
    if not _TAG_CATEGORY_MAP:
        meta_path = MODEL_DIR / "camie-tagger-v2-metadata.json"
        if meta_path.exists():
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                _TAG_CATEGORY_MAP = meta["dataset_info"]["tag_mapping"]["tag_to_category"]
            except Exception:
                _TAG_CATEGORY_MAP = {}
    t_low = tag_name.lower().strip()
    if t_low in _TAG_CATEGORY_MAP:
        return _TAG_CATEGORY_MAP[t_low]
    if t_low in DANBOORU_ARTIST_TO_FOLDER:
        return "artist"
    return "general"


def _get_cached_existing_folders(ttl_sec: float = 15.0) -> List[str]:
    global _EXISTING_FOLDERS_CACHE
    now = time.time()
    if now - _EXISTING_FOLDERS_CACHE[0] < ttl_sec and _EXISTING_FOLDERS_CACHE[1]:
        return _EXISTING_FOLDERS_CACHE[1]
    conn = get_db()
    folders = [r["folder"] for r in conn.execute("SELECT DISTINCT folder FROM images WHERE folder != ''").fetchall()]
    conn.close()
    _EXISTING_FOLDERS_CACHE = (now, folders)
    return folders


def match_existing_folder_by_artist(artist_name: str, twitter_handle: str = "") -> Optional[str]:
    """檢查搜到的畫師名稱或 Twitter @handle 是否對應使用者現有的繪師資料夾（內建記憶體 TTL 快取，零重複打庫）"""
    a_low = artist_name.lower().strip().lstrip("@")
    t_low = twitter_handle.lower().strip().lstrip("@")

    for cand in (a_low, t_low, re.sub(r"_?\d+$", "", a_low), re.sub(r"_?\d+$", "", t_low)):
        if cand and len(cand) >= 3 and cand in DANBOORU_ARTIST_TO_FOLDER:
            return DANBOORU_ARTIST_TO_FOLDER[cand]

    folders = _get_cached_existing_folders()
    for f in folders:
        if f in NON_ARTIST_FOLDERS:
            continue
        f_low = f.lower()
        if (
            a_low == f_low
            or (t_low and (t_low in f_low or (len(f_low) >= 4 and re.sub(r"_?\d+$", "", t_low) == f_low)))
            or (len(a_low) >= 4 and (a_low == f_low.replace(" ", "_") or (len(f_low) >= 4 and re.sub(r"_?\d+$", "", a_low) == f_low)))
        ):
            return f

    return None


def make_search_thumbnail(image_path: str, max_dim: int = 360) -> Optional[bytes]:
    with media_io(background=True):
        return _make_search_thumbnail(image_path, max_dim)


def _make_search_thumbnail(image_path: str, max_dim: int = 360) -> Optional[bytes]:
    try:
        if str(image_path).lower().endswith(".mp4"):
            from thumbnail_cache import _extract_video_thumbnail
            return _extract_video_thumbnail(Path(image_path), max_size=max_dim)
        with Image.open(image_path) as img:
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.thumbnail((max_dim, max_dim), Image.Resampling.BILINEAR)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=88)
            return buf.getvalue()
    except Exception:
        return None


def generate_google_lens_url(thumb_bytes: bytes) -> Optional[str]:
    """
    將圖片上傳至 Google Lens (https://lens.google.com/v3/upload)，
    取得 Google Lens 專屬視覺以圖搜圖結果網址 (vsrid URL)，供 Gemini Flash 3.8 MCP 或瀏覽器直接開啟比對。
    """
    try:
        resp = requests.post(
            "https://lens.google.com/v3/upload?hl=zh-TW",
            files={"encoded_image": ("image.jpg", thumb_bytes, "image/jpeg")},
            headers=HEADERS,
            timeout=10,
            allow_redirects=False,
        )
        loc = resp.headers.get("Location")
        if loc and loc.startswith("http"):
            return loc
    except Exception:
        pass
    return None


def _parse_danbooru_artist_entry(item: Dict[str, Any], fallback_name: str = "") -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "name": item.get("name") or fallback_name,
        "other_names": item.get("other_names") or [],
        "cjk_name": None,
        "twitter_handles": [],
        "pixiv_user_id": None,
        "urls": [],
        "resolved": True,
    }
    for oname in info["other_names"]:
        if RE_CJK.search(oname):
            info["cjk_name"] = oname
            break
    for u_obj in item.get("urls") or []:
        if not u_obj.get("is_active", True):
            continue
        u = u_obj.get("url", "")
        if not u:
            continue
        info["urls"].append(u)
        m_tw = RE_TWITTER_HANDLE.search(u)
        if m_tw:
            h = m_tw.group(1)
            if h.lower() not in ("i", "intent", "share", "home", "status", "search") and h not in info["twitter_handles"]:
                info["twitter_handles"].append(h)
        m_pu = RE_PIXIV_USER.search(u)
        if m_pu and not info["pixiv_user_id"]:
            info["pixiv_user_id"] = m_pu.group(1)
    return info


def fetch_danbooru_artist_by_url(url_query: str) -> Dict[str, Any]:
    """
    透過 Danbooru Artists URL 檢索 (`search[url_matches]`)：
    只要擁有畫師的 Pixiv User ID (`https://www.pixiv.net/users/21862577`)、
    Fanbox 帳號 (`https://www.fanbox.cc/@ricerice` 或 `https://ricerice.fanbox.cc`)、
    或 Twitter 帳號 (`https://twitter.com/handle`)，即可反查出 100% 認證的 Danbooru 畫師標籤、漢字名與 Twitter 帳號！
    """
    global _ARTIST_CACHE_DIRTY_COUNT
    _ensure_artist_cache_loaded()
    cache_key = f"url:{url_query.strip().lower()}"
    if cache_key in _DANBOORU_ARTIST_CACHE:
        return _DANBOORU_ARTIST_CACHE[cache_key]

    info: Dict[str, Any] = {
        "name": "",
        "other_names": [],
        "cjk_name": None,
        "twitter_handles": [],
        "pixiv_user_id": None,
        "urls": [],
        "resolved": True,
    }
    try:
        resp = requests.get(
            "https://danbooru.donmai.us/artists.json",
            params={
                "search[url_matches]": url_query.strip(),
                "only": "id,name,other_names,urls",
            },
            headers=BOORU_HEADERS,
            timeout=6,
        )
        if resp.status_code == 200:
            arr = resp.json()
            if isinstance(arr, list) and arr:
                info = _parse_danbooru_artist_entry(arr[0])
                if info["name"]:
                    _DANBOORU_ARTIST_CACHE[info["name"]] = info
    except Exception:
        pass

    _DANBOORU_ARTIST_CACHE[cache_key] = info
    _ARTIST_CACHE_DIRTY_COUNT += 1
    save_persistent_artist_cache(force=False)
    return info


def fetch_fanbox_post_info(post_id: str) -> Optional[Dict[str, Any]]:
    """
    透過 pixivFANBOX 官方公開 `post.info` API (`https://api.fanbox.cc/post.info?postId={id}`)：
    即使該篇貼文是付費限定 (`feeRequired > 0`, `isRestricted = True`)，
    依然能直接取得畫師的 Pixiv User ID (`user.userId`)、畫師名稱 (`user.name`)、
    Fanbox 專屬帳號 (`creatorId`)、文章標題與標籤，並自動串聯 Danbooru 反查完整畫師別名與 Twitter @帳號！
    """
    clean_pid = str(post_id).strip()
    if not clean_pid.isdigit():
        return None
    try:
        resp = requests.get(
            "https://api.fanbox.cc/post.info",
            params={"postId": clean_pid},
            headers=FANBOX_HEADERS,
            timeout=5,
        )
        if resp.status_code != 200:
            return None
        post = ((resp.json().get("body") or {}).get("post")) or {}
        if not post or not post.get("id"):
            return None
        user_obj = post.get("user") or {}
        pixiv_uid = str(user_obj.get("userId") or "").strip()
        user_name = str(user_obj.get("name") or "").strip()
        creator_id = str(post.get("creatorId") or "").strip()
        raw_tags = [str(t).strip() for t in (post.get("tags") or []) if str(t).strip()]

        artists: List[str] = []
        twitter_handles: List[str] = []
        cjk_name: Optional[str] = user_name if (user_name and RE_CJK.search(user_name)) else None

        # Cross-reference Danbooru Artist DB by Pixiv User ID and Fanbox Creator ID
        db_info = {}
        if pixiv_uid:
            db_info = fetch_danbooru_artist_by_url(f"https://www.pixiv.net/users/{pixiv_uid}")
        if not db_info.get("name") and creator_id:
            db_info = fetch_danbooru_artist_by_url(f"https://{creator_id}.fanbox.cc")
        if db_info.get("name"):
            artists.append(db_info["name"])
            if db_info.get("cjk_name") and not cjk_name:
                cjk_name = db_info["cjk_name"]
            for th in db_info.get("twitter_handles", []):
                if th not in twitter_handles:
                    twitter_handles.append(th)

        if user_name and user_name not in artists:
            artists.append(user_name)
        if creator_id and creator_id.lower() not in [a.lower() for a in artists]:
            artists.append(creator_id)

        source_url = f"https://{creator_id}.fanbox.cc/posts/{clean_pid}" if creator_id else f"https://www.fanbox.cc/posts/{clean_pid}"
        return {
            "fanbox_post_id": clean_pid,
            "pixiv_user_id": pixiv_uid or None,
            "creator_id": creator_id or None,
            "user_name": user_name or None,
            "cjk_name": cjk_name,
            "artists": artists,
            "twitter_handles": twitter_handles,
            "tags": raw_tags,
            "title": post.get("title") or "",
            "source_url": source_url,
        }
    except Exception:
        return None


def fetch_fxtwitter_status(status_id: str) -> Optional[Dict[str, Any]]:
    """
    針對檔名含有 18~19 位 Twitter 推文 ID (例如 `1686384323476529152.gif` 或 `..._2094385128068022654_photo.webp`)，
    透過 FxTwitter 公開 API 直接取得推文作者的 `@screen_name` 與顯示名稱，並串聯 Danbooru 畫師庫！
    """
    clean_sid = str(status_id).strip()
    if not clean_sid.isdigit():
        return None
    try:
        resp = requests.get(
            f"https://api.fxtwitter.com/status/{clean_sid}",
            headers=HEADERS,
            timeout=5,
        )
        if resp.status_code != 200:
            return None
        tweet = (resp.json().get("tweet")) or {}
        author = tweet.get("author") or {}
        screen_name = str(author.get("screen_name") or "").strip()
        disp_name = str(author.get("name") or "").strip()
        if not screen_name:
            return None

        artists: List[str] = []
        twitter_handles: List[str] = [screen_name]
        db_info = fetch_danbooru_artist_by_url(f"https://twitter.com/{screen_name}")
        if db_info.get("name"):
            artists.append(db_info["name"])
            for th in db_info.get("twitter_handles", []):
                if th not in twitter_handles:
                    twitter_handles.append(th)
        if disp_name and disp_name not in artists:
            artists.append(disp_name)
        return {
            "status_id": clean_sid,
            "screen_name": screen_name,
            "display_name": disp_name,
            "artists": artists,
            "twitter_handles": twitter_handles,
            "source_url": tweet.get("url") or f"https://x.com/{screen_name}/status/{clean_sid}",
        }
    except Exception:
        return None


def search_kemono_by_sha256(image_path: str) -> Optional[Dict[str, Any]]:
    """
    計算原圖 SHA256 雜湊並透過 Kemono 公開檔案雜湊索引 (`https://kemono.cr/api/v1/search_hash/{sha256}`)
    反查是否為 pixivFANBOX / Fantia / Patreon 付費貼文原檔，命中後自動解析畫師！
    """
    try:
        with media_io(background=True), open(image_path, "rb") as f:
            digest = hashlib.sha256()
            for block in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(block)
            sha_hex = digest.hexdigest()
        resp = requests.get(
            f"https://kemono.cr/api/v1/search_hash/{sha_hex}",
            headers={"User-Agent": HEADERS["User-Agent"], "Accept": "text/css"},
            timeout=5,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not isinstance(data, dict) or not data.get("posts"):
            return None
        post = data["posts"][0]
        service = str(post.get("service") or "").lower()
        user_id = str(post.get("user") or "").strip()
        post_id = str(post.get("id") or "").strip()
        if service == "fanbox" and post_id:
            fb_info = fetch_fanbox_post_info(post_id)
            if fb_info:
                fb_info["kemono_hit"] = True
                return fb_info
        if service == "fanbox" and user_id:
            db_info = fetch_danbooru_artist_by_url(f"https://www.pixiv.net/users/{user_id}")
            if db_info.get("name"):
                return {
                    "fanbox_post_id": post_id or None,
                    "pixiv_user_id": user_id,
                    "artists": [db_info["name"]],
                    "twitter_handles": db_info.get("twitter_handles", []),
                    "cjk_name": db_info.get("cjk_name"),
                    "tags": [],
                    "source_url": f"https://www.pixiv.net/users/{user_id}",
                    "kemono_hit": True,
                }
    except Exception:
        pass
    return None


def extract_platform_artist_clues_from_urls(urls: List[str]) -> Dict[str, Any]:
    """
    從 SauceNAO / Danbooru / IQDB 回傳的來源網址清單中，自動解析：
    - Fanbox Post ID (`*.fanbox.cc/posts/12345`) ➔ 呼叫 `fetch_fanbox_post_info`
    - Fanbox Creator ID (`fanbox.cc/@creator` 或 `creator.fanbox.cc`) ➔ 呼叫 `fetch_danbooru_artist_by_url`
    - Pixiv User ID (`pixiv.net/users/12345` 或 `pixiv.net/fanbox/creator/12345`) ➔ 呼叫 `fetch_danbooru_artist_by_url`
    - Fantia Club/Post (`fantia.jp/fanclubs/12345`) ➔ 呼叫 `fetch_danbooru_artist_by_url`
    """
    out: Dict[str, Any] = {
        "artists": [],
        "twitter_handles": [],
        "cjk_aliases": {},
        "fanbox_creators": [],
        "source_tags": [],
        "extra_urls": [],
        "is_fanbox": False,
        "is_fantia": False,
    }
    seen_queries = set()
    for u in urls:
        if not u:
            continue
        u_low = u.lower()
        if "fanbox" in u_low:
            out["is_fanbox"] = True
            if "Fanbox來源" not in out["source_tags"]:
                out["source_tags"].append("Fanbox來源")
        if "fantia.jp" in u_low:
            out["is_fantia"] = True
            if "Fantia來源" not in out["source_tags"]:
                out["source_tags"].append("Fantia來源")
        if "kemono." in u_low and "Kemono來源" not in out["source_tags"]:
            out["source_tags"].append("Kemono來源")

        m_fb_post = RE_FANBOX_POST.search(u)
        if m_fb_post:
            fb_pid = m_fb_post.group(1)
            q_key = f"fb_post:{fb_pid}"
            if q_key not in seen_queries:
                seen_queries.add(q_key)
                fb_info = fetch_fanbox_post_info(fb_pid)
                if fb_info:
                    out["is_fanbox"] = True
                    if "Fanbox來源" not in out["source_tags"]:
                        out["source_tags"].append("Fanbox來源")
                    if fb_info.get("creator_id") and fb_info["creator_id"] not in out["fanbox_creators"]:
                        out["fanbox_creators"].append(fb_info["creator_id"])
                    if fb_info.get("source_url") and fb_info["source_url"] not in out["extra_urls"]:
                        out["extra_urls"].append(fb_info["source_url"])
                    for a in fb_info.get("artists", []):
                        if a not in out["artists"]:
                            out["artists"].append(a)
                        if fb_info.get("cjk_name"):
                            out["cjk_aliases"][a] = fb_info["cjk_name"]
                    for th in fb_info.get("twitter_handles", []):
                        if th not in out["twitter_handles"]:
                            out["twitter_handles"].append(th)

        m_pu = RE_PIXIV_USER.search(u)
        if m_pu:
            uid = m_pu.group(1)
            q_key = f"pixiv_uid:{uid}"
            if q_key not in seen_queries:
                seen_queries.add(q_key)
                db_info = fetch_danbooru_artist_by_url(f"https://www.pixiv.net/users/{uid}")
                if db_info.get("name"):
                    a_name = db_info["name"]
                    if a_name not in out["artists"]:
                        out["artists"].append(a_name)
                    if db_info.get("cjk_name"):
                        out["cjk_aliases"][a_name] = db_info["cjk_name"]
                    for th in db_info.get("twitter_handles", []):
                        if th not in out["twitter_handles"]:
                            out["twitter_handles"].append(th)

        m_fc = RE_FANBOX_CREATOR.search(u)
        if m_fc:
            cid = (m_fc.group(1) or m_fc.group(2) or "").strip()
            if cid and cid.lower() not in ("www", "api", "downloads", "files"):
                out["is_fanbox"] = True
                if "Fanbox來源" not in out["source_tags"]:
                    out["source_tags"].append("Fanbox來源")
                if cid not in out["fanbox_creators"]:
                    out["fanbox_creators"].append(cid)
                q_key = f"fb_cid:{cid.lower()}"
                if q_key not in seen_queries:
                    seen_queries.add(q_key)
                    db_info = fetch_danbooru_artist_by_url(f"https://{cid}.fanbox.cc")
                    if db_info.get("name"):
                        a_name = db_info["name"]
                        if a_name not in out["artists"]:
                            out["artists"].append(a_name)
                        if db_info.get("cjk_name"):
                            out["cjk_aliases"][a_name] = db_info["cjk_name"]
                        for th in db_info.get("twitter_handles", []):
                            if th not in out["twitter_handles"]:
                                out["twitter_handles"].append(th)
                    elif cid not in out["artists"]:
                        out["artists"].append(cid)

        m_ft = RE_FANTIA_CLUB.search(u)
        if m_ft:
            out["is_fantia"] = True
            if "Fantia來源" not in out["source_tags"]:
                out["source_tags"].append("Fantia來源")
            ft_id = m_ft.group(1)
            q_key = f"fantia:{ft_id}"
            if q_key not in seen_queries:
                seen_queries.add(q_key)
                db_info = fetch_danbooru_artist_by_url(f"https://fantia.jp/fanclubs/{ft_id}")
                if db_info.get("name"):
                    a_name = db_info["name"]
                    if a_name not in out["artists"]:
                        out["artists"].append(a_name)
                    if db_info.get("cjk_name"):
                        out["cjk_aliases"][a_name] = db_info["cjk_name"]
                    for th in db_info.get("twitter_handles", []):
                        if th not in out["twitter_handles"]:
                            out["twitter_handles"].append(th)

    return out


def search_google_and_web_by_clues(
    pixiv_id: Optional[str] = None,
    filename: str = "",
    thumb_bytes: Optional[bytes] = None,
    image_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    多重線索與備份救援引擎：
    1. 若檔名符合 Fanbox 下載格式 (`{postId}_{page}.{ext}` 如 `12414466_1.webp`)，直接透過 `api.fanbox.cc/post.info` 取得 100% 認證畫師與 Pixiv UID！
    2. 若檔名含 18~19 位數 Twitter 推文 ID (如 `1686384323476529152.gif` 或 `..._2094385128068022654_photo.webp`)，透過 FxTwitter API 取得推主 `@帳號` 與 Danbooru 畫師！
    3. 若提供原圖路徑 (`image_path`)，自動透過 Kemono SHA256 原檔雜湊檢索是否為 Fanbox/Fantia 原圖！
    4. 當 Pixiv 作品已被刪除 (404) 時，自動透過 Yande.re / DuckDuckGo 搜尋備份畫師與標籤，並上傳 Google Lens。
    """
    res: Dict[str, Any] = {
        "hit": False,
        "artists": [],
        "cjk_aliases": {},
        "twitter_handles": [],
        "characters": [],
        "copyrights": [],
        "generals": [],
        "source_tags": [],
        "source_urls": [],
        "google_lens_url": None,
        "is_fanbox": False,
    }
    if thumb_bytes:
        res["google_lens_url"] = generate_google_lens_url(thumb_bytes)

    stem = os.path.splitext(filename or "")[0].strip()

    # 1. Check if filename is a Fanbox Post ID pattern (e.g. 12414466_1.webp)
    if stem and not pixiv_id:
        m_fb_fn = RE_FILENAME_FANBOX_POST.match(stem)
        if m_fb_fn:
            fb_info = fetch_fanbox_post_info(m_fb_fn.group(1))
            if fb_info and (fb_info.get("artists") or fb_info.get("twitter_handles")):
                res["hit"] = True
                res["is_fanbox"] = True
                if "Fanbox來源" not in res["source_tags"]:
                    res["source_tags"].append("Fanbox來源")
                for a in fb_info.get("artists", []):
                    if a not in res["artists"]:
                        res["artists"].append(a)
                    if fb_info.get("cjk_name"):
                        res["cjk_aliases"][a] = fb_info["cjk_name"]
                for th in fb_info.get("twitter_handles", []):
                    if th not in res["twitter_handles"]:
                        res["twitter_handles"].append(th)
                if fb_info.get("source_url"):
                    res["source_urls"].append(fb_info["source_url"])
                for rt in fb_info.get("tags", []):
                    cat = get_known_tag_category(rt)
                    if cat == "character" and rt not in res["characters"]:
                        res["characters"].append(rt)
                    elif cat == "copyright" and rt not in res["copyrights"]:
                        res["copyrights"].append(rt)
                    elif rt not in res["generals"]:
                        res["generals"].append(rt)

    # 2. Check if filename contains a Twitter handle + status ID or a 18~19 digit Twitter Status ID
    if stem and not res["artists"] and not res["twitter_handles"]:
        m_tw_hs = RE_FILENAME_TWITTER_HANDLE_STATUS.match(stem)
        if m_tw_hs:
            fn_handle = m_tw_hs.group(1)
            fn_sid = m_tw_hs.group(2)
            if not fn_handle.isdigit() and fn_handle.lower() not in ("twitter", "x", "video", "img", "image", "photo", "clip"):
                res["hit"] = True
                if fn_handle not in res["twitter_handles"]:
                    res["twitter_handles"].append(fn_handle)
                res["source_urls"].append(f"https://x.com/{fn_handle}/status/{fn_sid}")
                res_folder = resolve_artist_folder_name(fn_handle)
                if res_folder in FOLDER_TO_DANBOORU_ARTIST:
                    db_art = FOLDER_TO_DANBOORU_ARTIST[res_folder]
                    if db_art not in res["artists"]:
                        res["artists"].append(db_art)
                db_info = fetch_danbooru_artist_by_url(f"https://twitter.com/{fn_handle}")
                if db_info.get("name") and db_info["name"] not in res["artists"]:
                    res["artists"].append(db_info["name"])
                    if db_info.get("cjk_name"):
                        res["cjk_aliases"][db_info["name"]] = db_info["cjk_name"]
                    for th in db_info.get("twitter_handles", []):
                        if th not in res["twitter_handles"]:
                            res["twitter_handles"].append(th)

        m_tw_sid = RE_FILENAME_TWITTER_STATUS.search(stem)
        if m_tw_sid:
            tw_info = fetch_fxtwitter_status(m_tw_sid.group(1))
            if tw_info:
                res["hit"] = True
                for a in tw_info.get("artists", []):
                    if a not in res["artists"]:
                        res["artists"].append(a)
                    if tw_info.get("cjk_name"):
                        res["cjk_aliases"][a] = tw_info["cjk_name"]
                for th in tw_info.get("twitter_handles", []):
                    if th not in res["twitter_handles"]:
                        res["twitter_handles"].append(th)
                if tw_info.get("source_url") and tw_info["source_url"] not in res["source_urls"]:
                    res["source_urls"].append(tw_info["source_url"])

    # 3. Check Kemono SHA256 exact file hash for Fanbox / Fantia original files
    if image_path and not res["artists"] and os.path.exists(image_path):
        km_info = search_kemono_by_sha256(image_path)
        if km_info and (km_info.get("artists") or km_info.get("twitter_handles")):
            res["hit"] = True
            res["is_fanbox"] = True
            for st in ("Fanbox來源", "Kemono來源"):
                if st not in res["source_tags"]:
                    res["source_tags"].append(st)
            for a in km_info.get("artists", []):
                if a not in res["artists"]:
                    res["artists"].append(a)
                if km_info.get("cjk_name"):
                    res["cjk_aliases"][a] = km_info["cjk_name"]
            for th in km_info.get("twitter_handles", []):
                if th not in res["twitter_handles"]:
                    res["twitter_handles"].append(th)
            if km_info.get("source_url"):
                res["source_urls"].append(km_info["source_url"])

    if pixiv_id:
        # 4. Check Yande.re by source pixiv_id
        try:
            r_yd = requests.get(
                "https://yande.re/post.json",
                params={"tags": f"source:*{pixiv_id}*"},
                headers=BOORU_HEADERS,
                timeout=3.5,
            )
            if r_yd.status_code == 200:
                arr = r_yd.json()
                if isinstance(arr, list) and arr:
                    item = arr[0]
                    res["hit"] = True
                    if item.get("source"):
                        res["source_urls"].append(item["source"])
                        m_tw = RE_TWITTER_HANDLE.search(item["source"])
                        if m_tw and m_tw.group(1).lower() not in ("i", "intent", "share", "home"):
                            res["twitter_handles"].append(m_tw.group(1))
                    for t in (item.get("tags") or "").split():
                        cat = get_known_tag_category(t)
                        if cat == "artist" and t not in res["artists"]:
                            res["artists"].append(t)
                        elif cat == "character" and t not in res["characters"]:
                            res["characters"].append(t)
                        elif cat == "copyright" and t not in res["copyrights"]:
                            res["copyrights"].append(t)
                        elif cat == "general" and len(res["generals"]) < 30:
                            res["generals"].append(t)
        except Exception:
            pass

        # 5. Search web index (DuckDuckGo HTML) for "pixiv <pixiv_id>" or "artworks/<pixiv_id>"
        if not res["artists"] and not res["twitter_handles"]:
            try:
                q_str = f'"{pixiv_id}" pixiv OR danbooru OR twitter'
                r_web = requests.get(
                    "https://html.duckduckgo.com/html/",
                    params={"q": q_str},
                    headers=HEADERS,
                    timeout=3.5,
                )
                if r_web.status_code == 200:
                    text = r_web.text
                    for m_tw in RE_TWITTER_HANDLE.finditer(text):
                        h = m_tw.group(1)
                        if h.lower() not in ("i", "intent", "share", "home", "status", "search", "hashtag") and h not in res["twitter_handles"]:
                            res["twitter_handles"].append(h)
                            res["hit"] = True
                    for m_db in RE_DANBOORU_POST.finditer(text):
                        db_post = fetch_danbooru_post_details(m_db.group(1))
                        if db_post:
                            res["hit"] = True
                            res["artists"].extend(db_post.get("artists", []))
                            res["twitter_handles"].extend(db_post.get("twitter_handles", []))
                            res["characters"].extend(db_post.get("characters", []))
                            res["copyrights"].extend(db_post.get("copyrights", []))
                            res["generals"].extend(db_post.get("generals", [])[:30])
                            break
            except Exception:
                pass

    res["found"] = bool(res["hit"] and (res["artists"] or res["twitter_handles"] or res["characters"] or res["copyrights"] or res["generals"]))
    for th in res["twitter_handles"]:
        tw_tag = f"@{th}"
        if tw_tag not in res["artists"]:
            res["artists"].append(tw_tag)
    combined_tags: List[Tuple[str, str]] = []
    for ch in res["characters"]:
        combined_tags.append((ch, "character"))
    for cp in res["copyrights"]:
        combined_tags.append((cp, "copyright"))
    for g in res["generals"]:
        combined_tags.append((g, "general"))
    res["tags"] = combined_tags
    return res


def fetch_danbooru_artist_metadata(artist_name: str) -> Dict[str, Any]:
    """
    透過 Danbooru Artists API 查詢畫師的漢字/日文別名、Twitter @帳號與 Pixiv ID，
    並與本機持久化快取 (artist_cache.json) 雙向同步，相同畫師 0ms 秒速命中。
    """
    global _ARTIST_CACHE_DIRTY_COUNT
    _ensure_artist_cache_loaded()
    clean_name = artist_name.strip().lower().replace(" ", "_")
    if not clean_name:
        return {}
    if clean_name in _DANBOORU_ARTIST_CACHE:
        return _DANBOORU_ARTIST_CACHE[clean_name]

    info: Dict[str, Any] = {
        "name": "",
        "other_names": [],
        "cjk_name": None,
        "twitter_handles": [],
        "pixiv_user_id": None,
        "urls": [],
        "resolved": True,
    }
    try:
        resp = requests.get(
            "https://danbooru.donmai.us/artists.json",
            params={
                "search[name]": clean_name,
                "only": "id,name,other_names,urls",
            },
            headers=BOORU_HEADERS,
            timeout=8,
        )
        if resp.status_code == 200:
            arr = resp.json()
            if isinstance(arr, list) and arr:
                info = _parse_danbooru_artist_entry(arr[0], fallback_name=clean_name)
    except Exception:
        pass

    _DANBOORU_ARTIST_CACHE[clean_name] = info
    _ARTIST_CACHE_DIRTY_COUNT += 1
    save_persistent_artist_cache(force=False)
    return info


def fetch_danbooru_post_details(post_id: str) -> Optional[Dict[str, Any]]:
    """
    透過 Danbooru Post JSON API (https://danbooru.donmai.us/posts/{id}.json)
    取得 100% 結構化認證的 artist / character / copyright / general / rating / source。
    """
    try:
        resp = requests.get(
            f"https://danbooru.donmai.us/posts/{post_id}.json",
            headers=BOORU_HEADERS,
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        return parse_danbooru_post_json(data)
    except Exception:
        return None


def _file_md5(image_path):
    with media_io(background=True), open(image_path, "rb") as source:
        digest = hashlib.md5()
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
        return digest.hexdigest()


def search_danbooru_by_md5(image_path: str) -> Optional[Dict[str, Any]]:
    """若圖片檔案未經重新壓縮，直接以 MD5 在 Danbooru 秒速查出 100% 準確畫師與標籤"""
    try:
        md5_hex = _file_md5(image_path)
        resp = requests.get(
            "https://danbooru.donmai.us/posts.json",
            params={"tags": f"md5:{md5_hex}"},
            headers=BOORU_HEADERS,
            timeout=8,
        )
        if resp.status_code == 200:
            arr = resp.json()
            if isinstance(arr, list) and arr:
                return parse_danbooru_post_json(arr[0])
    except Exception:
        pass
    return None


def search_danbooru_by_md5_batch(
    id_to_path: Dict[int, str],
    chunk_size: int = 75,
) -> Dict[int, Dict[str, Any]]:
    """
    超高速 Danbooru 批次 MD5 直查：
    1 次 HTTP 請求可同時查詢最多 75~100 張圖片的 MD5，比逐張查詢快 80 倍以上！
    回傳 {image_id: parsed_danbooru_post_dict}
    """
    md5_to_img_ids: Dict[str, List[int]] = {}
    for iid, fp in id_to_path.items():
        try:
            if os.path.exists(fp):
                h = _file_md5(fp)
                md5_to_img_ids.setdefault(h, []).append(iid)
        except Exception:
            continue

    all_hashes = list(md5_to_img_ids.keys())
    matched_by_img_id: Dict[int, Dict[str, Any]] = {}
    if not all_hashes:
        return matched_by_img_id

    for i in range(0, len(all_hashes), chunk_size):
        batch_h = all_hashes[i : i + chunk_size]
        try:
            resp = requests.get(
                "https://danbooru.donmai.us/posts.json",
                params={"tags": f"md5:{','.join(batch_h)}", "limit": 100},
                headers=BOORU_HEADERS,
                timeout=10,
            )
            if resp.status_code == 200:
                arr = resp.json()
                if isinstance(arr, list):
                    for post_data in arr:
                        post_md5 = (post_data.get("md5") or "").lower()
                        if post_md5 in md5_to_img_ids:
                            parsed = parse_danbooru_post_json(post_data)
                            for iid in md5_to_img_ids[post_md5]:
                                matched_by_img_id[iid] = parsed
        except Exception:
            continue

    save_persistent_artist_cache(force=True)
    return matched_by_img_id


def search_danbooru_by_pixiv_ids_batch(
    pixiv_ids: List[str],
    chunk_size: int = 40,
) -> Dict[str, Dict[str, Any]]:
    """
    超高速 Danbooru 批次 Pixiv ID 備份直查 (`tags=pixiv:id1,id2,...`)：
    即使原作者已在 Pixiv 刪除作品 (404)，仍可從 Danbooru 批次救回 100% 認證畫師、Twitter @帳號、角色與完整標籤！
    回傳 {pixiv_id_str: parsed_danbooru_post_dict}
    """
    unique_pids = [str(p).strip() for p in dict.fromkeys(pixiv_ids) if str(p).strip().isdigit()]
    matched_by_pid: Dict[str, Dict[str, Any]] = {}
    if not unique_pids:
        return matched_by_pid

    for i in range(0, len(unique_pids), chunk_size):
        batch_p = unique_pids[i : i + chunk_size]
        try:
            resp = requests.get(
                "https://danbooru.donmai.us/posts.json",
                params={"tags": f"pixiv:{','.join(batch_p)}", "limit": 100},
                headers=BOORU_HEADERS,
                timeout=10,
            )
            if resp.status_code == 200:
                arr = resp.json()
                if isinstance(arr, list):
                    for post_data in arr:
                        pid_val = str(post_data.get("pixiv_id") or "")
                        if pid_val and pid_val not in matched_by_pid:
                            matched_by_pid[pid_val] = parse_danbooru_post_json(post_data)
        except Exception:
            continue

    save_persistent_artist_cache(force=True)
    return matched_by_pid



def parse_danbooru_post_json(data: Dict[str, Any]) -> Dict[str, Any]:
    artists = [a for a in (data.get("tag_string_artist") or "").split() if a]
    characters = [c for c in (data.get("tag_string_character") or "").split() if c]
    copyrights = [c for c in (data.get("tag_string_copyright") or "").split() if c]
    generals = [g for g in (data.get("tag_string_general") or "").split() if g]
    rating_code = (data.get("rating") or "").lower()
    rating = RATING_MAP.get(rating_code)
    source = data.get("source") or ""
    pixiv_id = str(data["pixiv_id"]) if data.get("pixiv_id") else None

    twitter_handles = []
    if source:
        m_tw = RE_TWITTER_HANDLE.search(source)
        if m_tw and m_tw.group(1).lower() not in ("i", "intent", "share", "home"):
            twitter_handles.append(m_tw.group(1))
        if not pixiv_id:
            m_px = RE_PIXIV_ARTWORK.search(source)
            if m_px:
                pixiv_id = m_px.group(1)

    artist_details = []
    for art in artists:
        ainfo = fetch_danbooru_artist_metadata(art)
        if ainfo:
            artist_details.append(ainfo)
            for th in ainfo.get("twitter_handles", []):
                if th not in twitter_handles:
                    twitter_handles.append(th)

    return {
        "post_id": data.get("id"),
        "artists": artists,
        "artist_details": artist_details,
        "twitter_handles": twitter_handles,
        "characters": characters,
        "copyrights": copyrights,
        "generals": generals,
        "rating": rating,
        "source": source,
        "pixiv_id": pixiv_id,
    }


def fetch_yandere_post_details(post_id: str) -> Optional[Dict[str, Any]]:
    try:
        resp = requests.get(
            "https://yande.re/post.json",
            params={"tags": f"id:{post_id}"},
            headers=BOORU_HEADERS,
            timeout=8,
        )
        if resp.status_code == 200:
            arr = resp.json()
            if isinstance(arr, list) and arr:
                item = arr[0]
                src = item.get("source") or ""
                tw_handles = []
                m_tw = RE_TWITTER_HANDLE.search(src)
                if m_tw and m_tw.group(1).lower() not in ("i", "intent", "share", "home"):
                    tw_handles.append(m_tw.group(1))
                px_id = None
                m_px = RE_PIXIV_ARTWORK.search(src)
                if m_px:
                    px_id = m_px.group(1)
                return {
                    "source": src,
                    "twitter_handles": tw_handles,
                    "pixiv_id": px_id,
                    "rating": RATING_MAP.get((item.get("rating") or "").lower()),
                    "tags": (item.get("tags") or "").split(),
                }
    except Exception:
        pass
    return None


def search_saucenao(thumb_bytes: bytes, min_similarity: float = 72.0, api_key: Optional[str] = None) -> Dict[str, Any]:
    """
    透過 SauceNAO 以圖搜圖，支援抓出最新 Twitter (@handle)、Pixiv、Danbooru、Fantia/Fanbox 畫師與角色！
    """
    result = {
        "hit": False,
        "similarity": 0.0,
        "artists": [],
        "twitter_handles": [],
        "characters": [],
        "copyrights": [],
        "source_urls": [],
        "pixiv_id": None,
        "danbooru_post_ids": [],
        "rate_limited": False,
    }

    try:
        url = "https://saucenao.com/search.php"
        params = {}
        if api_key:
            params["api_key"] = api_key
            params["output_type"] = 2

        resp = requests.post(
            url,
            params=params,
            files={"file": ("thumb.jpg", thumb_bytes, "image/jpeg")},
            headers=HEADERS,
            timeout=7,
        )

        if resp.status_code == 429 or "Too Many Requests" in resp.text or "Daily Search Limit Exceeded" in resp.text:
            result["rate_limited"] = True
            return result

        if resp.status_code != 200:
            return result

        if api_key and resp.headers.get("content-type", "").startswith("application/json"):
            data = resp.json()
            for item in data.get("results", []):
                hdr = item.get("header", {})
                sim = float(hdr.get("similarity", 0.0))
                if sim < min_similarity:
                    continue
                result["hit"] = True
                result["similarity"] = max(result["similarity"], sim)
                d = item.get("data", {})
                for k in ("creator", "member_name", "author_name", "pawoo_user_display_name", "twitter_user_handle"):
                    val = d.get(k)
                    if isinstance(val, list):
                        for v in val:
                            if v and v not in result["artists"]:
                                result["artists"].append(str(v).strip())
                    elif isinstance(val, str) and val.strip():
                        if val.strip() not in result["artists"]:
                            result["artists"].append(val.strip())
                if d.get("material"):
                    for m in str(d["material"]).split(","):
                        m_clean = m.strip()
                        if m_clean and m_clean not in result["copyrights"]:
                            result["copyrights"].append(m_clean)
                if d.get("characters"):
                    for c in str(d["characters"]).split(","):
                        c_clean = c.strip()
                        if c_clean and c_clean not in result["characters"]:
                            result["characters"].append(c_clean)
                if d.get("danbooru_id"):
                    result["danbooru_post_ids"].append(str(d["danbooru_id"]))
                for u in d.get("ext_urls", []):
                    if u not in result["source_urls"]:
                        result["source_urls"].append(u)
                    m_tw = RE_TWITTER_HANDLE.search(u)
                    if m_tw and m_tw.group(1).lower() not in ("i", "intent", "share", "home") and m_tw.group(1) not in result["twitter_handles"]:
                        result["twitter_handles"].append(m_tw.group(1))
                    m_px = RE_PIXIV_ARTWORK.search(u)
                    if m_px and not result["pixiv_id"]:
                        result["pixiv_id"] = m_px.group(1)
                    m_db = RE_DANBOORU_POST.search(u)
                    if m_db and m_db.group(1) not in result["danbooru_post_ids"]:
                        result["danbooru_post_ids"].append(m_db.group(1))
            return result

        soup = BeautifulSoup(resp.text, "html.parser")
        for res_div in soup.select("div.result"):
            sim_div = res_div.select_one("div.resultsimilarityinfo")
            content_div = res_div.select_one("div.resultcontent")
            if not sim_div or not content_div:
                continue
            m_sim = RE_SIMILARITY.search(sim_div.text)
            if not m_sim:
                continue
            sim = float(m_sim.group(1))
            if sim < min_similarity:
                continue

            result["hit"] = True
            result["similarity"] = max(result["similarity"], sim)

            for a_tag in content_div.select("a"):
                href = a_tag.get("href", "").strip()
                txt = a_tag.text.strip()
                if href and href.startswith("http"):
                    if href not in result["source_urls"]:
                        result["source_urls"].append(href)
                    m_tw = RE_TWITTER_HANDLE.search(href)
                    if m_tw:
                        handle = m_tw.group(1)
                        if handle.lower() not in ("i", "intent", "share", "home") and handle not in result["twitter_handles"]:
                            result["twitter_handles"].append(handle)
                    m_px = RE_PIXIV_ARTWORK.search(href)
                    if m_px and not result["pixiv_id"]:
                        result["pixiv_id"] = m_px.group(1)
                    m_db = RE_DANBOORU_POST.search(href)
                    if m_db and m_db.group(1) not in result["danbooru_post_ids"]:
                        result["danbooru_post_ids"].append(m_db.group(1))
                if txt.startswith("Twitter @"):
                    h = txt.replace("Twitter @", "").strip()
                    if h and h not in result["twitter_handles"]:
                        result["twitter_handles"].append(h)

            strings = list(content_div.stripped_strings)
            for i, s in enumerate(strings):
                s_low = s.lower()
                if s_low in ("creator:", "author:", "member:", "artist:") and i + 1 < len(strings):
                    raw_creators = strings[i + 1]
                    for part in raw_creators.split(","):
                        p_clean = part.strip()
                        if p_clean and p_clean.lower() not in ("virtual youtuber", "original") and p_clean not in result["artists"]:
                            result["artists"].append(p_clean)
                elif s_low in ("material:", "source:") and i + 1 < len(strings):
                    val = strings[i + 1].strip()
                    if s_low == "material:" and val:
                        for part in val.split(","):
                            p_clean = part.strip()
                            if p_clean and p_clean not in result["copyrights"]:
                                result["copyrights"].append(p_clean)
                elif s_low in ("characters:", "character:") and i + 1 < len(strings):
                    val = strings[i + 1].strip()
                    for part in val.split(","):
                        p_clean = part.strip()
                        if p_clean and p_clean not in result["characters"]:
                            result["characters"].append(p_clean)

    except Exception:
        pass

    return result


def search_iqdb(thumb_bytes: bytes, min_similarity: float = 73.0) -> Dict[str, Any]:
    """
    透過 IQDB 以圖搜圖，並提取對應的 Danbooru / Yande.re / Gelbooru 網址與標籤！
    """
    result = {
        "hit": False,
        "similarity": 0.0,
        "rating": None,
        "tags": [],
        "source_urls": [],
        "danbooru_post_ids": [],
        "yandere_post_ids": [],
    }
    try:
        resp = requests.post(
            "https://iqdb.org/",
            files={"file": ("thumb.jpg", thumb_bytes, "image/jpeg")},
            headers=HEADERS,
            timeout=7,
        )
        if resp.status_code != 200:
            return result

        soup = BeautifulSoup(resp.text, "html.parser")
        for table in soup.select("div.pages > div > table"):
            table_text = " ".join(table.stripped_strings)
            if "Your image" in table_text or "No relevant matches" in table_text:
                continue

            m_sim = RE_SIMILARITY.search(table_text)
            if not m_sim:
                continue
            sim = float(m_sim.group(1))
            if sim < min_similarity:
                continue

            result["hit"] = True
            result["similarity"] = max(result["similarity"], sim)

            for a_tag in table.select("a"):
                href = a_tag.get("href", "").strip()
                if href:
                    if href.startswith("//"):
                        href = "https:" + href
                    if href not in result["source_urls"]:
                        result["source_urls"].append(href)
                    m_db = RE_DANBOORU_POST.search(href)
                    if m_db and m_db.group(1) not in result["danbooru_post_ids"]:
                        result["danbooru_post_ids"].append(m_db.group(1))
                    m_yd = RE_YANDERE_POST.search(href)
                    if m_yd and m_yd.group(1) not in result["yandere_post_ids"]:
                        result["yandere_post_ids"].append(m_yd.group(1))

            img_el = table.select_one("img")
            if img_el:
                alt_text = img_el.get("alt") or img_el.get("title") or ""
                m_alt = RE_IQDB_ALT.search(alt_text)
                if m_alt:
                    r_code = m_alt.group(1).lower()
                    if r_code in RATING_MAP and not result["rating"]:
                        result["rating"] = RATING_MAP[r_code]
                    raw_tags = m_alt.group(2).strip().split()
                    for rt in raw_tags:
                        if rt and rt not in result["tags"]:
                            result["tags"].append(rt)

    except Exception:
        pass

    return result


def calibrate_image_tags_in_db(
    conn,
    image_id: int,
    has_verified_artist: bool = False,
    has_verified_character: bool = False,
    has_verified_copyright: bool = False,
    remove_tag_names: Optional[List[str]] = None,
) -> int:
    """
    標籤校正器：
    - 當圖片已透過以圖搜圖 / Danbooru / Pixiv / Gemini Flash 3.8 MCP 確認真實畫師時，
      自動清除該圖片上所有由本機 AI (`source='ai'`) 猜測的畫師標籤！
    - 當已確認真實角色/作品時，清除本機 AI 低信心度 (< 0.55) 猜測的角色/作品標籤。
    - 支援透過 `remove_tag_names` 精準刪除特定錯誤標籤。
    """
    removed = 0
    if has_verified_artist:
        cur = conn.execute(
            """
            DELETE FROM image_tags
            WHERE image_id = ?
              AND source = 'ai'
              AND tag_id IN (SELECT id FROM tags WHERE category = 'artist')
            """,
            (image_id,),
        )
        removed += cur.rowcount

    if has_verified_character:
        cur = conn.execute(
            """
            DELETE FROM image_tags
            WHERE image_id = ?
              AND source = 'ai'
              AND confidence < 0.70
              AND tag_id IN (SELECT id FROM tags WHERE category = 'character')
            """,
            (image_id,),
        )
        removed += cur.rowcount

    if has_verified_copyright:
        cur = conn.execute(
            """
            DELETE FROM image_tags
            WHERE image_id = ?
              AND source = 'ai'
              AND confidence < 0.70
              AND tag_id IN (SELECT id FROM tags WHERE category = 'copyright')
            """,
            (image_id,),
        )
        removed += cur.rowcount

    if remove_tag_names:
        for tname in remove_tag_names:
            cur = conn.execute(
                """
                DELETE FROM image_tags
                WHERE image_id = ? AND source != 'manual'
                  AND tag_id IN (SELECT id FROM tags WHERE name = ? OR zh_name = ?)
                """,
                (image_id, tname.strip(), tname.strip()),
            )
            removed += cur.rowcount

    return removed


def apply_danbooru_posts_batch_to_db(
    id_to_post: Dict[int, Dict[str, Any]],
    extra_source_tags_by_id: Optional[Dict[int, List[Tuple[str, str, str]]]] = None,
    refresh_counts: bool = False,
    source_label: str = "danbooru",
    purge_ai_artist: bool = True,
) -> Dict[str, Any]:
    """
    將批次查獲的 Danbooru Post 結構化資料 (來自批次 MD5 或批次 Pixiv ID 查詢)
    在純記憶體組裝後，透過單一短事務批次寫入 SQLite，零長事務鎖庫！
    """
    from database import run_db_write_with_retry
    if not id_to_post and not extra_source_tags_by_id:
        return {
            "images_updated": 0,
            "updated": 0,
            "tags_linked": 0,
            "purged_ai": 0,
            "purged_ai_tags": 0,
            "suggested_folders": {},
            "suggested_moves": {},
        }

    # Pre-resolve folders and collect all tag tuples in memory
    per_img_tags: Dict[int, List[Tuple[str, str, float, str]]] = {}
    cjk_aliases: Dict[str, str] = {}
    sug_folder_by_id: Dict[int, Optional[str]] = {}
    pixiv_by_id: Dict[int, Optional[str]] = {}
    verified_flags: Dict[int, Tuple[bool, bool, bool]] = {}
    eff_src = source_label or "danbooru"

    for iid, db_post in id_to_post.items():
        t_list: List[Tuple[str, str, float, str]] = []
        sug_f = None
        has_art = False
        has_char = False
        has_copy = False

        for ainfo in db_post.get("artist_details", []):
            if ainfo.get("cjk_name") and ainfo.get("name"):
                cjk_aliases[ainfo["name"]] = ainfo["cjk_name"]

        for art in db_post.get("artists", []):
            t_list.append((art, "artist", 1.0, eff_src))
            has_art = True
            matched_f = match_existing_folder_by_artist(art)
            if matched_f and not sug_f:
                sug_f = matched_f

        for th in db_post.get("twitter_handles", []):
            tw_tag = f"@{th}"
            t_list.append((tw_tag, "artist", 1.0, "twitter"))
            has_art = True
            if not sug_f:
                sug_f = match_existing_folder_by_artist(th, th)

        if not sug_f and db_post.get("artists"):
            sug_f = resolve_artist_folder_name(db_post["artists"][0])
        elif not sug_f and db_post.get("twitter_handles"):
            sug_f = resolve_artist_folder_name(db_post["twitter_handles"][0])

        for ch in db_post.get("characters", []):
            t_list.append((ch, "character", 1.0, eff_src))
            has_char = True

        for cp in db_post.get("copyrights", []):
            t_list.append((cp, "copyright", 1.0, eff_src))
            has_copy = True

        for gen in db_post.get("generals", [])[:45]:
            t_list.append((gen, "general", 0.98, eff_src))

        if db_post.get("rating"):
            t_list.append((db_post["rating"], "rating", 1.0, eff_src))

        if db_post.get("pixiv_id"):
            t_list.append(("Pixiv來源", "source", 1.0, "pixiv"))
            pixiv_by_id[iid] = str(db_post["pixiv_id"])

        per_img_tags[iid] = t_list
        sug_folder_by_id[iid] = sug_f
        verified_flags[iid] = (has_art, has_char, has_copy)

    if extra_source_tags_by_id:
        for iid, extra_list in extra_source_tags_by_id.items():
            lst = per_img_tags.setdefault(iid, [])
            for t_name, t_cat, t_src in extra_list:
                lst.append((t_name, t_cat, 1.0, t_src))

    conn = get_db()
    tag_cache: Dict[str, int] = {}
    for r in conn.execute("SELECT id, name FROM tags"):
        tag_cache[r["name"]] = r["id"]

    stats: Dict[str, Any] = {"images_updated": 0, "tags_linked": 0, "purged_ai": 0}

    def _write_tx(c):
        stats.update(images_updated=0,tags_linked=0,purged_ai=0)
        links = []
        img_updates = []
        for iid, t_list in per_img_tags.items():
            flags = verified_flags.get(iid)
            if purge_ai_artist and flags and any(flags):
                stats["purged_ai"] += calibrate_image_tags_in_db(
                    c,
                    image_id=iid,
                    has_verified_artist=flags[0],
                    has_verified_character=flags[1],
                    has_verified_copyright=flags[2],
                )
            for tname, tcat, tconf, tsrc in t_list:
                tid = ensure_tag(c, tag_cache, tname, tcat, source=tsrc)
                if tid > 0:
                    links.append((iid, tid, tconf, tsrc))
                    if tname in cjk_aliases:
                        c.execute(
                            "UPDATE tags SET zh_name = COALESCE(NULLIF(zh_name, ''), ?) WHERE id = ?",
                            (cjk_aliases[tname], tid),
                        )
            if iid in id_to_post:
                img_updates.append((pixiv_by_id.get(iid), sug_folder_by_id.get(iid), iid))
                stats["images_updated"] += 1

        if links:
            c.executemany(
                """
                INSERT INTO image_tags (image_id, tag_id, confidence, source)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(image_id, tag_id) DO UPDATE SET
                confidence = MAX(image_tags.confidence, excluded.confidence),
                source = CASE
                        WHEN image_tags.source = 'manual' THEN image_tags.source
                    WHEN excluded.source IN ('danbooru', 'pixiv', 'saucenao', 'iqdb', 'twitter', 'gemini_mcp') THEN excluded.source
                    ELSE image_tags.source
                END
                """,
                links,
            )
            stats["tags_linked"] += len(links)

        if img_updates:
            c.executemany(
                "UPDATE images SET pixiv_id = COALESCE(?, pixiv_id), suggested_folder = COALESCE(?, suggested_folder) WHERE id = ?",
                img_updates,
            )

        if refresh_counts:
            refresh_tag_counts(c)

    try:
        run_db_write_with_retry(conn, _write_tx)
    finally:
        conn.close()

    sug_folders_clean = {k: v for k, v in sug_folder_by_id.items() if v}
    sug_moves: Dict[str, List[int]] = {}
    for iid, fname in sug_folders_clean.items():
        sug_moves.setdefault(fname, []).append(iid)

    stats["updated"] = stats["images_updated"]
    stats["purged_ai_tags"] = stats["purged_ai"]
    stats["suggested_folders"] = sug_folders_clean
    stats["suggested_moves"] = sug_moves
    return stats


def reverse_search_and_tag_image(
    image_id: int,
    min_similarity: float = 72.0,
    saucenao_api_key: Optional[str] = None,
    fallback_to_ai: bool = False,
    include_google_lens_url: bool = True,
    precomputed_db_post: Optional[Dict[str, Any]] = None,
    run_refresh_counts: bool = True,
) -> Dict[str, Any]:
    """
    針對單張圖片執行「多引擎深度以圖搜圖 + Danbooru/Pixiv/Twitter/Google 結構化認證 + 標籤校正」：
    1. 讀取圖片路徑後立即釋放資料庫連線（網路請求期間 0 鎖庫！）。
    2. 先檢查 Danbooru MD5 直查與 Pixiv ID 反查（100% 準確）。
    3. 執行 IQDB 與 SauceNAO 以圖搜圖。
    4. 若仍未找到且含 Pixiv ID 或啟用 Google 以圖搜圖，自動執行 Google Lens 上傳與 Yande.re/Web 搜尋救援。
    5. 所有標籤在記憶體組裝完畢後，以單一毫秒級短事務寫入 SQLite。
    """
    from database import run_db_write_with_retry

    conn = get_db()
    row = conn.execute(
        "SELECT id, rel_path, filename, folder, pixiv_id FROM images WHERE id = ?",
        (image_id,),
    ).fetchone()
    conn.close()
    if not row:
        return {"found": False, "error": "Image not found"}

    row_dict = dict(row)
    full_path = str(GALLERY_ROOT / row_dict["rel_path"])
    if not os.path.exists(full_path):
        return {"found": False, "error": "File not found on disk"}

    thumb_bytes = make_search_thumbnail(full_path, max_dim=360)
    if not thumb_bytes:
        return {"found": False, "error": "Failed to read image"}

    google_lens_url = None

    # Step 1: Try Danbooru MD5 or Pixiv ID exact match first (fast & free)
    db_post = precomputed_db_post or search_danbooru_by_md5(full_path)
    if not db_post and row_dict.get("pixiv_id"):
        px_map = search_danbooru_by_pixiv_ids_batch([str(row_dict["pixiv_id"])])
        db_post = px_map.get(str(row_dict["pixiv_id"]))

    iq_res = {
        "hit": False,
        "similarity": 0.0,
        "rating": None,
        "tags": [],
        "danbooru_post_ids": [],
        "yandere_post_ids": [],
        "source_urls": [],
    }
    sn_res = {
        "hit": False,
        "rate_limited": False,
        "similarity": 0.0,
        "artists": [],
        "twitter_handles": [],
        "pixiv_id": None,
        "danbooru_post_ids": [],
        "characters": [],
        "copyrights": [],
        "source_urls": [],
    }
    web_res = {
        "hit": False,
        "artists": [],
        "cjk_aliases": {},
        "twitter_handles": [],
        "characters": [],
        "copyrights": [],
        "generals": [],
        "source_tags": [],
        "source_urls": [],
        "google_lens_url": None,
    }

    # Step 1b: Fast-path check for Fanbox Post ID or Twitter Status ID in filename (0.2s, saves SauceNAO quota!)
    if not db_post:
        stem = os.path.splitext(row_dict["filename"])[0]
        if RE_FILENAME_FANBOX_POST.match(stem) or RE_FILENAME_TWITTER_STATUS.search(stem):
            web_res = search_google_and_web_by_clues(
                pixiv_id=row_dict.get("pixiv_id"),
                filename=row_dict["filename"],
                thumb_bytes=None,
                image_path=None,
            )

    # Step 2: Only run IQDB & SauceNAO if Danbooru or fast-path filename clue didn't already give a verified match
    if not db_post and not web_res["hit"]:
        from concurrent.futures import ThreadPoolExecutor
        ex = ThreadPoolExecutor(max_workers=2)
        try:
            fut_iq = ex.submit(search_iqdb, thumb_bytes, min_similarity)
            fut_sn = ex.submit(search_saucenao, thumb_bytes, min_similarity, saucenao_api_key)
            try:
                iq_res = fut_iq.result(timeout=6.0)
            except Exception:
                pass
            try:
                sn_res = fut_sn.result(timeout=6.0)
            except Exception:
                pass
        finally:
            ex.shutdown(wait=False, cancel_futures=True)

        # Step 3: If Danbooru post ID found via IQDB or SauceNAO, fetch full structured Danbooru Post + Artist JSON!
        candidate_db_ids = iq_res.get("danbooru_post_ids", []) + sn_res.get("danbooru_post_ids", [])
        for db_pid in candidate_db_ids:
            db_post = fetch_danbooru_post_details(db_pid)
            if db_post:
                break

        # Step 3b: If SauceNAO returned a Pixiv ID, check Danbooru by Pixiv ID too
        if not db_post and sn_res.get("pixiv_id"):
            px_map = search_danbooru_by_pixiv_ids_batch([str(sn_res["pixiv_id"])])
            db_post = px_map.get(str(sn_res["pixiv_id"]))

    # Step 4: If Yande.re post ID found via IQDB, fetch Yande.re JSON for source URL (Twitter/Pixiv)
    yd_post = None
    if not db_post and iq_res.get("yandere_post_ids"):
        yd_post = fetch_yandere_post_details(iq_res["yandere_post_ids"][0])

    # Step 5: If still no artist found (e.g. deleted Pixiv 404, Fanbox/Kemono or obscure Twitter work), run full clues + Kemono SHA256 + Google Lens fallback!
    if not db_post and not sn_res["hit"] and not iq_res["hit"] and not web_res["hit"]:
        web_res = search_google_and_web_by_clues(
            pixiv_id=row_dict.get("pixiv_id"),
            filename=row_dict["filename"],
            thumb_bytes=thumb_bytes if include_google_lens_url else None,
            image_path=full_path,
        )
        if web_res.get("google_lens_url"):
            google_lens_url = web_res["google_lens_url"]

    if include_google_lens_url and not google_lens_url:
        google_lens_url = generate_google_lens_url(thumb_bytes)

    all_sources = []
    if db_post and db_post.get("source"):
        all_sources.append(db_post["source"])
    if yd_post and yd_post.get("source"):
        all_sources.append(yd_post["source"])
    for u in sn_res["source_urls"] + iq_res["source_urls"] + web_res["source_urls"]:
        if u and u not in all_sources:
            all_sources.append(u)

    # Step 6: Extract Fanbox / Fantia / Kemono / Pixiv User clues from all discovered URLs (SauceNAO, IQDB, etc.)
    platform_clues = extract_platform_artist_clues_from_urls(all_sources) if all_sources else {
        "artists": [],
        "cjk_aliases": {},
        "twitter_handles": [],
        "pixiv_user_ids": [],
        "fanbox_Wait": [],
        "source_tags": [],
        "extra_urls": [],
    }
    for eu in platform_clues.get("extra_urls", []):
        if eu not in all_sources:
            all_sources.append(eu)

    # Assemble all tags in pure memory (zero DB lock!)
    raw_tags_to_add: List[Tuple[str, str, float, str]] = []  # (name, category, confidence, source)
    detected_artists: List[str] = []
    twitter_handles: List[str] = []
    detected_characters: List[str] = []
    detected_copyrights: List[str] = []
    cjk_artist_aliases: Dict[str, str] = {}
    cjk_artist_aliases.update(web_res.get("cjk_aliases", {}))
    cjk_artist_aliases.update(platform_clues.get("cjk_aliases", {}))
    suggested_folder = None

    best_sim = max(
        100.0 if db_post else 0.0,
        sn_res["similarity"],
        iq_res["similarity"],
        95.0 if (web_res["hit"] or platform_clues.get("artists")) else 0.0,
    )
    conf_val = round(best_sim / 100.0, 4) if best_sim > 0 else 0.96

    # --- A. Process Structured Danbooru Post + Artist API (Highest Accuracy!) ---
    if db_post:
        for ainfo in db_post.get("artist_details", []):
            if ainfo.get("cjk_name") and ainfo.get("name"):
                cjk_artist_aliases[ainfo["name"]] = ainfo["cjk_name"]

        for art in db_post["artists"]:
            raw_tags_to_add.append((art, "artist", 1.0, "danbooru"))
            detected_artists.append(art)
            matched_f = match_existing_folder_by_artist(art)
            if matched_f and not suggested_folder:
                suggested_folder = matched_f

        for th in db_post.get("twitter_handles", []):
            if th not in twitter_handles:
                twitter_handles.append(th)

        for ch in db_post["characters"]:
            raw_tags_to_add.append((ch, "character", 1.0, "danbooru"))
            detected_characters.append(ch)

        for cp in db_post["copyrights"]:
            raw_tags_to_add.append((cp, "copyright", 1.0, "danbooru"))
            detected_copyrights.append(cp)

        for gen in db_post["generals"][:45]:
            raw_tags_to_add.append((gen, "general", 0.98, "danbooru"))

        if db_post.get("rating"):
            raw_tags_to_add.append((db_post["rating"], "rating", 1.0, "danbooru"))

    # --- A2. Process Platform Clues (Fanbox / Fantia / Kemono / Danbooru Artist URL Lookup) ---
    for art in platform_clues.get("artists", []):
        raw_tags_to_add.append((art, "artist", 1.0, "danbooru"))
        if art not in detected_artists:
            detected_artists.append(art)
        matched_f = match_existing_folder_by_artist(art)
        if matched_f and not suggested_folder:
            suggested_folder = matched_f

    for th in platform_clues.get("twitter_handles", []):
        if th not in twitter_handles:
            twitter_handles.append(th)

    # --- B. Process SauceNAO Results ---
    if sn_res["hit"]:
        for art in sn_res["artists"]:
            art_norm = art.replace(" ", "_").lower() if art.isascii() else art
            # Skip generic Fanbox creator handle if platform_clues already resolved the canonical Danbooru artist
            if platform_clues.get("artists") and any(
                art_norm == fc.lower() for fc in platform_clues.get("fanbox_creators", [])
            ) and art_norm not in platform_clues.get("artists", []):
                continue
            raw_tags_to_add.append((art_norm, "artist", conf_val, "saucenao"))
            if art_norm not in detected_artists:
                detected_artists.append(art_norm)
            matched_f = match_existing_folder_by_artist(art_norm) or match_existing_folder_by_artist(art)
            if matched_f and not suggested_folder:
                suggested_folder = matched_f

        for th in sn_res["twitter_handles"]:
            if th not in twitter_handles:
                twitter_handles.append(th)

        for cp in sn_res["copyrights"]:
            cp_norm = cp.lower().replace(" ", "_")
            raw_tags_to_add.append((cp_norm, "copyright", conf_val, "saucenao"))
            if cp_norm not in detected_copyrights:
                detected_copyrights.append(cp_norm)

        for ch in sn_res["characters"]:
            ch_norm = ch.lower().replace(" ", "_")
            raw_tags_to_add.append((ch_norm, "character", conf_val, "saucenao"))
            if ch_norm not in detected_characters:
                detected_characters.append(ch_norm)

    # --- C. Process Yande.re & Google/Web/Fanbox/FxTwitter Fallback Results ---
    if yd_post:
        for th in yd_post.get("twitter_handles", []):
            if th not in twitter_handles:
                twitter_handles.append(th)

    if web_res["hit"]:
        for art in web_res["artists"]:
            raw_tags_to_add.append((art, "artist", 0.98, "danbooru"))
            if art not in detected_artists:
                detected_artists.append(art)
            matched_f = match_existing_folder_by_artist(art)
            if matched_f and not suggested_folder:
                suggested_folder = matched_f
        for th in web_res["twitter_handles"]:
            if th not in twitter_handles:
                twitter_handles.append(th)
        for ch in web_res["characters"]:
            raw_tags_to_add.append((ch, "character", 0.90, "iqdb"))
            if ch not in detected_characters:
                detected_characters.append(ch)
        for cp in web_res["copyrights"]:
            raw_tags_to_add.append((cp, "copyright", 0.90, "iqdb"))
            if cp not in detected_copyrights:
                detected_copyrights.append(cp)
        for gen in web_res["generals"][:30]:
            raw_tags_to_add.append((gen, "general", 0.88, "iqdb"))

    # --- D. Process Twitter Handles & Resolve Artist Folder ---
    for tw_handle in twitter_handles:
        tw_tag = f"@{tw_handle}"
        raw_tags_to_add.append((tw_tag, "artist", 1.0, "twitter"))
        if tw_tag not in detected_artists:
            detected_artists.append(tw_tag)
        matched_f = match_existing_folder_by_artist(tw_handle, tw_handle)
        if matched_f and not suggested_folder:
            suggested_folder = matched_f

    # --- E. Process IQDB Fallback Tags (when Danbooru JSON wasn't already fetched) ---
    if iq_res["hit"] and not db_post:
        if iq_res["rating"]:
            raw_tags_to_add.append((iq_res["rating"], "rating", 1.0, "iqdb"))

        for tname in iq_res["tags"]:
            cat = get_known_tag_category(tname)
            if any(tname.lower() == a.lower().replace(" ", "_") for a in sn_res["artists"]):
                cat = "artist"
            raw_tags_to_add.append((tname, cat, conf_val, "iqdb"))
            if cat == "artist":
                if tname not in detected_artists:
                    detected_artists.append(tname)
                matched_f = match_existing_folder_by_artist(tname)
                if matched_f and not suggested_folder:
                    suggested_folder = matched_f

    # --- F. Resolve Best Artist Folder Name ---
    if not suggested_folder and detected_artists:
        non_handle_artists = [a for a in detected_artists if not a.startswith("@")]
        primary_artist = non_handle_artists[0] if non_handle_artists else detected_artists[0].lstrip("@")
        suggested_folder = resolve_artist_folder_name(primary_artist)

    new_pixiv_id = (
        (db_post.get("pixiv_id") if db_post else None)
        or sn_res.get("pixiv_id")
        or (yd_post.get("pixiv_id") if yd_post else None)
        or row_dict["pixiv_id"]
    )

    if any("twitter.com" in u or "x.com" in u for u in all_sources) or twitter_handles:
        raw_tags_to_add.append(("Twitter來源(以圖搜圖)", "source", 1.0, "saucenao"))
    if any("pixiv.net/artworks" in u or "illust_id=" in u for u in all_sources) or new_pixiv_id:
        raw_tags_to_add.append(("Pixiv來源", "source", 1.0, "saucenao"))
    for st in set(web_res.get("source_tags", []) + platform_clues.get("source_tags", [])):
        raw_tags_to_add.append((st, "source", 1.0, "saucenao"))

    if raw_tags_to_add:
        conn_w = get_db()
        tag_cache: Dict[str, int] = {}
        for r in conn_w.execute("SELECT id, name FROM tags"):
            tag_cache[r["name"]] = r["id"]

        purged_box = [0]
        links_count_box = [0]

        def _commit_single(c):
            purged_box[0] = calibrate_image_tags_in_db(
                c,
                image_id=image_id,
                has_verified_artist=bool(detected_artists),
                has_verified_character=bool(detected_characters),
                has_verified_copyright=bool(detected_copyrights),
            )
            links = []
            for tname, tcat, tconf, tsrc in raw_tags_to_add:
                tid = ensure_tag(c, tag_cache, tname, tcat, source=tsrc)
                if tid > 0:
                    links.append((image_id, tid, tconf, tsrc))
                    if tname in cjk_artist_aliases:
                        c.execute(
                            "UPDATE tags SET zh_name = COALESCE(NULLIF(zh_name, ''), ?) WHERE id = ?",
                            (cjk_artist_aliases[tname], tid),
                        )
            if links:
                c.executemany(
                    """
                    INSERT INTO image_tags (image_id, tag_id, confidence, source)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(image_id, tag_id) DO UPDATE SET
                    confidence = MAX(image_tags.confidence, excluded.confidence),
                    source = CASE
                        WHEN image_tags.source = 'manual' THEN image_tags.source
                        WHEN excluded.source IN ('danbooru', 'pixiv', 'saucenao', 'iqdb', 'twitter', 'gemini_mcp') THEN excluded.source
                        ELSE image_tags.source
                    END
                    """,
                    links,
                )
                links_count_box[0] = len(links)
            c.execute(
                "UPDATE images SET pixiv_id = COALESCE(?, pixiv_id), suggested_folder = COALESCE(?, suggested_folder) WHERE id = ?",
                (new_pixiv_id, suggested_folder, image_id),
            )
            if run_refresh_counts:
                refresh_tag_counts(c)

        try:
            run_db_write_with_retry(conn_w, _commit_single)
        finally:
            conn_w.close()

        return {
            "found": True,
            "method": "danbooru_verified" if db_post else ("google_web_rescued" if web_res["hit"] else "reverse_image_search"),
            "danbooru_hit": bool(db_post),
            "saucenao_hit": sn_res["hit"],
            "iqdb_hit": iq_res["hit"],
            "web_hit": web_res["hit"],
            "similarity": best_sim,
            "artists": detected_artists,
            "cjk_artist_aliases": cjk_artist_aliases,
            "twitter_handles": twitter_handles,
            "characters": detected_characters,
            "copyrights": detected_copyrights,
            "suggested_folder": suggested_folder,
            "tags_added": links_count_box[0],
            "purged_wrong_ai_tags": purged_box[0],
            "source_urls": all_sources[:6],
            "google_lens_url": google_lens_url,
        }

    # Optional Fallback to Local GPU AI Tagger ONLY for general content tags (never pollute artist with low-conf AI guesses)
    if fallback_to_ai:
        from ai_tagger import tagger_engine, preprocess_image_fast
        tagger_engine.ensure_model_loaded()
        tensor = preprocess_image_fast(full_path, 512)
        if tensor is not None:
            preds, ai_sug_folder = tagger_engine.predict_single(tensor, artist_thresh=0.75)
            conn2 = get_db()
            tag_cache2: Dict[str, int] = {}
            for r in conn2.execute("SELECT id, name FROM tags"):
                tag_cache2[r["name"]] = r["id"]
            ai_links_count = [0]

            def _commit_ai_fallback(c):
                from processing_state import record,AI_MODEL_VERSION
                ai_links = []
                for tname, cat, conf in preds:
                    tid = ensure_tag(c, tag_cache2, tname, cat, source="ai")
                    if tid > 0:
                        ai_links.append((image_id, tid, round(conf, 4), "ai"))
                if ai_links:
                    c.executemany(
                        "INSERT OR IGNORE INTO image_tags (image_id, tag_id, confidence, source) VALUES (?, ?, ?, ?)",
                        ai_links,
                    )
                    ai_links_count[0] = len(ai_links)
                c.execute(
                    "UPDATE images SET suggested_folder = COALESCE(?, suggested_folder) WHERE id = ?",
                    (ai_sug_folder, image_id),
                )
                record(c,image_id,"ai","succeeded",model_version=AI_MODEL_VERSION)
                if run_refresh_counts:
                    refresh_tag_counts(c)

            try:
                run_db_write_with_retry(conn2, _commit_ai_fallback)
            finally:
                conn2.close()
            return {
                "found": False,
                "method": "ai_content_fallback",
                "saucenao_hit": False,
                "iqdb_hit": False,
                "suggested_folder": ai_sug_folder,
                "tags_added": ai_links_count[0],
                "google_lens_url": google_lens_url,
            }

    return {
        "found": False,
        "method": "none",
        "rate_limited": sn_res.get("rate_limited", False),
        "google_lens_url": google_lens_url,
    }


class BatchReverseSearchWorker:
    def __init__(self):
        self.running = False
        self.stop_requested = False
        self.status_message = "待機中"
        self.total_target = 0
        self.processed_count = 0
        self.hits_count = 0
        self.moved_count = 0
        self.calibrated_count = 0
        self.current_file = ""
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def start(
        self,
        scope: str = "root_untagged",
        limit: int = 50,
        min_similarity: float = 72.0,
        saucenao_api_key: Optional[str] = None,
        fallback_to_ai: bool = False,
        delay_sec: float = 1.2,
        auto_move_to_artist_folder: bool = False,
    ) -> Dict[str, Any]:
        with self._lock:
            if self.running:
                return {"status": "already_running"}
            try:
                lease = acquire_job("批次以圖搜圖")
            except BackgroundBusy as exc:
                return exc.response()
            self.running = True
            self.stop_requested = False

        self._thread = threading.Thread(
            target=self._run_loop,
            args=(
                scope,
                limit,
                min_similarity,
                saucenao_api_key,
                fallback_to_ai,
                delay_sec,
                auto_move_to_artist_folder,
            ),
            kwargs={"_job_lease": lease},
            daemon=True,
        )
        lease.publish(force=True,recipe={"kind":"reverse","options":{"scope":scope,"limit":limit,"min_similarity":min_similarity,"fallback_to_ai":fallback_to_ai,"delay_sec":delay_sec,"auto_move_to_artist_folder":auto_move_to_artist_folder}})
        try:
            start_job_thread(self._thread, lease)
        except BaseException:
            self.running = False
            raise
        return {
            "status": "started",
            "limit": limit,
            "auto_move_to_artist_folder": auto_move_to_artist_folder,
        }

    def stop(self) -> Dict[str, Any]:
        self.stop_requested = True
        self.status_message = "正在停止以圖搜圖任務..."
        return {"status": "stopping"}

    def get_status(self) -> Dict[str, Any]:
        return {
            "running": self.running,
            "status_message": self.status_message,
            "total_target": self.total_target,
            "processed_count": self.processed_count,
            "hits_count": self.hits_count,
            "moved_count": self.moved_count,
            "calibrated_count": self.calibrated_count,
            "current_file": self.current_file,
        }

    @exclusive_job("批次以圖搜圖")
    def _run_loop(
        self,
        scope: str,
        limit: int,
        min_similarity: float,
        saucenao_api_key: Optional[str],
        fallback_to_ai: bool,
        delay_sec: float,
        auto_move_to_artist_folder: bool = False,
    ):
        try:
            conn = get_db()
            where = "WHERE media_type IN ('image', 'gif', 'video')"
            verified_subquery = """
                SELECT it.image_id FROM image_tags it
                JOIN tags t ON t.id = it.tag_id
                WHERE t.category = 'artist'
                  AND it.source IN ('pixiv', 'saucenao', 'iqdb', 'danbooru', 'twitter', 'gemini_mcp', 'folder')
            """
            if scope == "root_untagged":
                where += f" AND folder = '' AND id NOT IN ({verified_subquery})"
            elif scope == "twitter_raw":
                where += f" AND folder = '' AND id IN (SELECT image_id FROM image_tags it JOIN tags t ON t.id=it.tag_id WHERE t.name='Twitter原始檔名') AND id NOT IN ({verified_subquery})"
            elif scope == "ai_low_conf":
                where += f" AND id IN (SELECT it.image_id FROM image_tags it JOIN tags t ON t.id=it.tag_id WHERE t.category='artist' AND it.source='ai') AND id NOT IN ({verified_subquery})"
            elif scope == "all_untagged":
                where += f" AND id NOT IN ({verified_subquery})"

            sql = f"SELECT id, rel_path, filename, folder FROM images {where} ORDER BY mtime DESC"
            if limit and limit > 0:
                sql += f" LIMIT {int(limit)}"
            rows = conn.execute(sql).fetchall()
            conn.close()

            self.total_target = len(rows)
            self.processed_count = 0
            self.hits_count = 0
            self.moved_count = 0
            self.calibrated_count = 0

            if not rows:
                self.status_message = "✅ 所有目標圖片皆已具備認證畫師，無需以圖搜圖"
                self.running = False
                return

            t_rev_start = time.time()
            # Phase 1: Batch Danbooru MD5 pre-filter (up to 75 images per request!)
            self.status_message = f"⚡ 正在執行 Danbooru 批次 MD5 高速預篩 ({len(rows)} 張)..."
            id_to_path = {r["id"]: str(GALLERY_ROOT / r["rel_path"]) for r in rows}
            batch_md5_hits = search_danbooru_by_md5_batch(id_to_path)
            if batch_md5_hits:
                db_stats = apply_danbooru_posts_batch_to_db(batch_md5_hits, refresh_counts=False)
                self.hits_count += len(batch_md5_hits)
                self.calibrated_count += db_stats.get("purged_ai", 0)

            pending_moves: Dict[str, List[int]] = {}
            for r in rows:
                if self.stop_requested:
                    break
                iid = r["id"]
                self.current_file = r["filename"]
                if iid in batch_md5_hits:
                    sug_f = (db_stats.get("suggested_folders") or {}).get(iid)
                    if auto_move_to_artist_folder and sug_f and r["folder"] == "":
                        pending_moves.setdefault(sug_f, []).append(iid)
                    self.processed_count += 1
                    update_job(processed_count=self.processed_count,total_target=self.total_target,stage=self.status_message)
                    continue

                self.status_message = (
                    f"正在執行以圖搜圖與 Google 救援校正 — 進度：{self.processed_count:,} / {self.total_target:,} 張 ({r['filename']})"
                )

                res = reverse_search_and_tag_image(
                    image_id=iid,
                    min_similarity=min_similarity,
                    saucenao_api_key=saucenao_api_key,
                    fallback_to_ai=fallback_to_ai,
                    include_google_lens_url=True,
                    run_refresh_counts=False,
                )
                if res.get("found"):
                    self.hits_count += 1
                    self.calibrated_count += int(res.get("purged_wrong_ai_tags", 0))

                sug_folder = res.get("suggested_folder")
                if auto_move_to_artist_folder and sug_folder and r["folder"] == "" and res.get("artists"):
                    pending_moves.setdefault(sug_folder, []).append(iid)

                self.processed_count += 1

                update_job(processed_count=self.processed_count,total_target=self.total_target,stage=self.status_message)
                if not self.stop_requested and self.processed_count < self.total_target:
                    time.sleep(max(0.6, delay_sec))

            if auto_move_to_artist_folder and pending_moves and not self.stop_requested:
                from gallery_service import move_images_to_folder
                for target_f, iids in pending_moves.items():
                    m_res = move_images_to_folder(iids, target_f, auto_rename=True, refresh_counts=False)
                    self.moved_count += m_res.get("moved", 0)

            conn_f = get_db()
            with conn_f:
                refresh_tag_counts(conn_f)
            conn_f.close()

            rev_elapsed = round(time.time() - t_rev_start, 2)
            if self.stop_requested:
                self.status_message = (
                    f"已暫停以圖搜圖 (完成 {self.processed_count} 張，認證 {self.hits_count} 位畫師，"
                    f"校正 {self.calibrated_count} 個錯誤AI標籤，歸檔 {self.moved_count} 張)"
                )
            else:
                self.status_message = (
                    f"✅ 深度以圖搜圖完成！處理 {self.processed_count} 張，認證 {self.hits_count} 位畫師，"
                    f"清除 {self.calibrated_count} 個錯誤AI猜測標籤"
                    + (f"，並歸檔 {self.moved_count} 張" if auto_move_to_artist_folder else "")
                )
            log_operation(
                category="search_calibrate",
                action="批次以圖搜圖與畫師校正",
                summary=self.status_message,
                status="warning" if self.stop_requested else "success",
                affected_count=self.processed_count,
                duration_sec=rev_elapsed,
                details={
                    "scope": scope,
                    "processed_count": self.processed_count,
                    "hits_count": self.hits_count,
                    "calibrated_count": self.calibrated_count,
                    "moved_count": self.moved_count,
                },
            )
        except Exception as e:
            self.status_message = f"❌ 以圖搜圖錯誤: {e}"
            update_job(status="failed",last_error=str(e))
            log_operation(
                category="search_calibrate",
                action="批次以圖搜圖與畫師校正",
                summary=self.status_message,
                status="error",
                details={"scope": scope, "error": str(e)},
            )
        finally:
            self.running = False
            if self.stop_requested:
                update_job(status="cancelled",stage="已停止")


reverse_search_worker = BatchReverseSearchWorker()
