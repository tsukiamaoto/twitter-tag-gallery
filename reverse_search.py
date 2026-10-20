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
import time
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageFile

from database import (
    GALLERY_ROOT,
    MODEL_DIR,
    NON_ARTIST_FOLDERS,
    get_db,
    ensure_tag,
    refresh_tag_counts,
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
RE_PIXIV_ARTWORK = re.compile(r"pixiv\.net/(?:[a-z]{2}/)?artworks/(\d+)", re.IGNORECASE)
RE_PIXIV_USER = re.compile(r"pixiv\.net/(?:[a-z]{2}/)?users/(\d+)", re.IGNORECASE)
RE_DANBOORU_POST = re.compile(r"danbooru\.donmai\.us/posts/(\d+)", re.IGNORECASE)
RE_YANDERE_POST = re.compile(r"yande\.re/post/show/(\d+)", re.IGNORECASE)
RE_IQDB_ALT = re.compile(r"Rating:\s*([gsqe])\s+Score:\s*\S+\s+Tags:\s*(.+)$", re.IGNORECASE)
RE_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]")

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

# Lazy-loaded tag category dictionary from Camie metadata if available
_TAG_CATEGORY_MAP: Dict[str, str] = {}
_DANBOORU_ARTIST_CACHE: Dict[str, Dict[str, Any]] = {}


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


def match_existing_folder_by_artist(artist_name: str, twitter_handle: str = "") -> Optional[str]:
    """檢查搜到的畫師名稱或 Twitter @handle 是否對應使用者現有的繪師資料夾"""
    a_low = artist_name.lower().strip()
    t_low = twitter_handle.lower().strip().lstrip("@")

    if a_low in DANBOORU_ARTIST_TO_FOLDER:
        return DANBOORU_ARTIST_TO_FOLDER[a_low]
    if t_low and t_low in DANBOORU_ARTIST_TO_FOLDER:
        return DANBOORU_ARTIST_TO_FOLDER[t_low]

    conn = get_db()
    folders = [r["folder"] for r in conn.execute("SELECT DISTINCT folder FROM images WHERE folder != ''").fetchall()]
    conn.close()

    for f in folders:
        if f in NON_ARTIST_FOLDERS:
            continue
        f_low = f.lower()
        if a_low == f_low or (t_low and t_low in f_low) or (len(a_low) >= 4 and a_low == f_low.replace(" ", "_")):
            return f

    return None


def make_search_thumbnail(image_path: str, max_dim: int = 360) -> Optional[bytes]:
    try:
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
            timeout=12,
            allow_redirects=False,
        )
        loc = resp.headers.get("Location")
        if loc and loc.startswith("http"):
            return loc
    except Exception:
        pass
    return None


def fetch_danbooru_artist_metadata(artist_name: str) -> Dict[str, Any]:
    """
    透過 Danbooru Artists API 查詢畫師的：
    - 漢字/日文/中文常用名稱 (other_names)
    - 官方 Twitter @帳號 (x.com / twitter.com URLs)
    - 官方 Pixiv User ID
    - Fanbox / Patreon 連結
    """
    clean_name = artist_name.strip().lower().replace(" ", "_")
    if not clean_name:
        return {}
    if clean_name in _DANBOORU_ARTIST_CACHE:
        return _DANBOORU_ARTIST_CACHE[clean_name]

    info: Dict[str, Any] = {
        "name": clean_name,
        "other_names": [],
        "cjk_name": None,
        "twitter_handles": [],
        "pixiv_user_id": None,
        "urls": [],
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
                item = arr[0]
                other_names = item.get("other_names") or []
                info["other_names"] = other_names
                for oname in other_names:
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
                        if h.lower() not in ("i", "intent", "share", "home") and h not in info["twitter_handles"]:
                            info["twitter_handles"].append(h)
                    m_pu = RE_PIXIV_USER.search(u)
                    if m_pu and not info["pixiv_user_id"]:
                        info["pixiv_user_id"] = m_pu.group(1)
    except Exception:
        pass

    _DANBOORU_ARTIST_CACHE[clean_name] = info
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


def search_danbooru_by_md5(image_path: str) -> Optional[Dict[str, Any]]:
    """若圖片檔案未經重新壓縮，直接以 MD5 在 Danbooru 秒速查出 100% 準確畫師與標籤"""
    try:
        md5_hex = hashlib.md5(Path(image_path).read_bytes()).hexdigest()
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
                WHERE image_id = ?
                  AND tag_id IN (SELECT id FROM tags WHERE name = ? OR zh_name = ?)
                """,
                (image_id, tname.strip(), tname.strip()),
            )
            removed += cur.rowcount

    return removed


def reverse_search_and_tag_image(
    image_id: int,
    min_similarity: float = 72.0,
    saucenao_api_key: Optional[str] = None,
    fallback_to_ai: bool = False,
    include_google_lens_url: bool = True,
) -> Dict[str, Any]:
    """
    針對單張圖片執行「多引擎深度以圖搜圖 + Danbooru/Pixiv/Twitter 結構化認證 + 標籤校正」：
    1. 先檢查 Danbooru MD5 直查與 Pixiv ID 反查（100% 準確）。
    2. 執行 IQDB 與 SauceNAO 以圖搜圖。
    3. 一旦 IQDB 或 SauceNAO 命中 Danbooru / Yande.re 貼文，立即呼叫 Danbooru Post JSON API 與 Artist API，
       取得 100% 認證的畫師名稱、Twitter @帳號、漢字別名、角色、作品與完整標籤。
    4. 自動執行「標籤校正 (Tag Calibration)」，清除該圖片上舊有的錯誤 AI 畫師猜測 (`source='ai'`)。
    5. 產生 Google Lens 以圖搜圖網址 (`google_lens_url`)，供 Gemini Flash 3.8 MCP 進行二次驗證或人工檢視。
    """
    conn = get_db()
    row = conn.execute(
        "SELECT id, rel_path, filename, folder, pixiv_id FROM images WHERE id = ?",
        (image_id,),
    ).fetchone()
    if not row:
        conn.close()
        return {"found": False, "error": "Image not found"}

    full_path = str(GALLERY_ROOT / row["rel_path"])
    if not os.path.exists(full_path):
        conn.close()
        return {"found": False, "error": "File not found on disk"}

    thumb_bytes = make_search_thumbnail(full_path, max_dim=360)
    if not thumb_bytes:
        conn.close()
        return {"found": False, "error": "Failed to read image"}

    google_lens_url = generate_google_lens_url(thumb_bytes) if include_google_lens_url else None

    # Step 1: Try Danbooru MD5 exact match first (fast & free)
    db_post = search_danbooru_by_md5(full_path)

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

    # Step 2: Only run IQDB & SauceNAO if MD5 didn't already give a 100% Danbooru match
    if not db_post:
        from concurrent.futures import ThreadPoolExecutor
        ex = ThreadPoolExecutor(max_workers=2)
        try:
            fut_iq = ex.submit(search_iqdb, thumb_bytes, min_similarity)
            fut_sn = ex.submit(search_saucenao, thumb_bytes, min_similarity, saucenao_api_key)
            try:
                iq_res = fut_iq.result(timeout=5.0)
            except Exception:
                pass
            try:
                sn_res = fut_sn.result(timeout=5.0)
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

    # Step 4: If Yande.re post ID found via IQDB, fetch Yande.re JSON for source URL (Twitter/Pixiv)
    yd_post = None
    if not db_post and iq_res.get("yandere_post_ids"):
        yd_post = fetch_yandere_post_details(iq_res["yandere_post_ids"][0])

    tag_cache: Dict[str, int] = {}
    for r in conn.execute("SELECT id, name FROM tags"):
        tag_cache[r["name"]] = r["id"]

    links = []
    detected_artists = []
    twitter_handles = []
    detected_characters = []
    detected_copyrights = []
    cjk_artist_aliases: Dict[str, str] = {}
    suggested_folder = None

    best_sim = max(
        100.0 if db_post else 0.0,
        sn_res["similarity"],
        iq_res["similarity"],
    )
    conf_val = round(best_sim / 100.0, 4) if best_sim > 0 else 0.96

    # --- A. Process Structured Danbooru Post + Artist API (Highest Accuracy!) ---
    if db_post:
        for ainfo in db_post.get("artist_details", []):
            if ainfo.get("cjk_name"):
                cjk_artist_aliases[ainfo["name"]] = ainfo["cjk_name"]

        for art in db_post["artists"]:
            tid = ensure_tag(conn, tag_cache, art, "artist")
            if tid > 0:
                links.append((image_id, tid, 1.0, "danbooru"))
                detected_artists.append(art)
                if art in cjk_artist_aliases:
                    conn.execute(
                        "UPDATE tags SET zh_name = COALESCE(zh_name, ?) WHERE id = ?",
                        (cjk_artist_aliases[art], tid),
                    )
            matched_f = match_existing_folder_by_artist(art)
            if matched_f and not suggested_folder:
                suggested_folder = matched_f

        for th in db_post.get("twitter_handles", []):
            if th not in twitter_handles:
                twitter_handles.append(th)

        for ch in db_post["characters"]:
            tid = ensure_tag(conn, tag_cache, ch, "character")
            if tid > 0:
                links.append((image_id, tid, 1.0, "danbooru"))
                detected_characters.append(ch)

        for cp in db_post["copyrights"]:
            tid = ensure_tag(conn, tag_cache, cp, "copyright")
            if tid > 0:
                links.append((image_id, tid, 1.0, "danbooru"))
                detected_copyrights.append(cp)

        for gen in db_post["generals"][:45]:
            tid = ensure_tag(conn, tag_cache, gen, "general")
            if tid > 0:
                links.append((image_id, tid, 0.98, "danbooru"))

        if db_post.get("rating"):
            tid_r = ensure_tag(conn, tag_cache, db_post["rating"], "rating")
            if tid_r > 0:
                links.append((image_id, tid_r, 1.0, "danbooru"))

    # --- B. Process SauceNAO Results ---
    if sn_res["hit"]:
        for art in sn_res["artists"]:
            art_norm = art.replace(" ", "_").lower() if art.isascii() else art
            tid = ensure_tag(conn, tag_cache, art_norm, "artist")
            if tid > 0:
                links.append((image_id, tid, conf_val, "saucenao"))
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
            tid = ensure_tag(conn, tag_cache, cp_norm, "copyright")
            if tid > 0:
                links.append((image_id, tid, conf_val, "saucenao"))
                if cp_norm not in detected_copyrights:
                    detected_copyrights.append(cp_norm)

        for ch in sn_res["characters"]:
            ch_norm = ch.lower().replace(" ", "_")
            tid = ensure_tag(conn, tag_cache, ch_norm, "character")
            if tid > 0:
                links.append((image_id, tid, conf_val, "saucenao"))
                if ch_norm not in detected_characters:
                    detected_characters.append(ch_norm)

    # --- C. Process Yande.re Source Handles ---
    if yd_post:
        for th in yd_post.get("twitter_handles", []):
            if th not in twitter_handles:
                twitter_handles.append(th)

    # --- D. Process Twitter Handles & Resolve Artist Folder ---
    for tw_handle in twitter_handles:
        tw_tag = f"@{tw_handle}"
        tid_tw = ensure_tag(conn, tag_cache, tw_tag, "artist")
        if tid_tw > 0:
            links.append((image_id, tid_tw, 1.0, "twitter"))
            if tw_tag not in detected_artists:
                detected_artists.append(tw_tag)
        matched_f = match_existing_folder_by_artist(tw_handle, tw_handle)
        if matched_f and not suggested_folder:
            suggested_folder = matched_f

    # --- E. Process IQDB Fallback Tags (when Danbooru JSON wasn't already fetched) ---
    if iq_res["hit"] and not db_post:
        if iq_res["rating"]:
            tid_r = ensure_tag(conn, tag_cache, iq_res["rating"], "rating")
            if tid_r > 0:
                links.append((image_id, tid_r, 1.0, "iqdb"))

        for tname in iq_res["tags"]:
            cat = get_known_tag_category(tname)
            if any(tname.lower() == a.lower().replace(" ", "_") for a in sn_res["artists"]):
                cat = "artist"
            tid = ensure_tag(conn, tag_cache, tname, cat)
            if tid > 0:
                links.append((image_id, tid, conf_val, "iqdb"))
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
        or row["pixiv_id"]
    )

    all_sources = []
    if db_post and db_post.get("source"):
        all_sources.append(db_post["source"])
    for u in sn_res["source_urls"] + iq_res["source_urls"]:
        if u not in all_sources:
            all_sources.append(u)

    if any("twitter.com" in u or "x.com" in u for u in all_sources) or twitter_handles:
        tid_src = ensure_tag(conn, tag_cache, "Twitter來源(以圖搜圖)", "source")
        links.append((image_id, tid_src, 1.0, "saucenao"))
    if any("pixiv.net" in u for u in all_sources) or new_pixiv_id:
        tid_src = ensure_tag(conn, tag_cache, "Pixiv來源", "source")
        links.append((image_id, tid_src, 1.0, "saucenao"))

    if links:
        with conn:
            # Calibrate tags: purge inaccurate AI artist/character/copyright guesses!
            purged_ai_tags = calibrate_image_tags_in_db(
                conn,
                image_id=image_id,
                has_verified_artist=bool(detected_artists),
                has_verified_character=bool(detected_characters),
                has_verified_copyright=bool(detected_copyrights),
            )
            conn.executemany(
                """
                INSERT INTO image_tags (image_id, tag_id, confidence, source)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(image_id, tag_id) DO UPDATE SET
                confidence = MAX(image_tags.confidence, excluded.confidence),
                source = CASE
                    WHEN excluded.source IN ('danbooru', 'pixiv', 'saucenao', 'iqdb', 'twitter', 'gemini_mcp') THEN excluded.source
                    ELSE image_tags.source
                END
                """,
                links,
            )
            conn.execute(
                "UPDATE images SET ai_tagged = 1, pixiv_id = COALESCE(?, pixiv_id), suggested_folder = COALESCE(?, suggested_folder) WHERE id = ?",
                (new_pixiv_id, suggested_folder, image_id),
            )
            refresh_tag_counts(conn)
        conn.close()
        return {
            "found": True,
            "method": "danbooru_verified" if db_post else "reverse_image_search",
            "danbooru_hit": bool(db_post),
            "saucenao_hit": sn_res["hit"],
            "iqdb_hit": iq_res["hit"],
            "similarity": best_sim,
            "artists": detected_artists,
            "cjk_artist_aliases": cjk_artist_aliases,
            "twitter_handles": twitter_handles,
            "characters": detected_characters,
            "copyrights": detected_copyrights,
            "suggested_folder": suggested_folder,
            "tags_added": len(links),
            "purged_wrong_ai_tags": purged_ai_tags,
            "source_urls": all_sources[:6],
            "google_lens_url": google_lens_url,
        }

    conn.close()

    # Optional Fallback to Local GPU AI Tagger ONLY for general content tags (never pollute artist with low-conf AI guesses)
    if fallback_to_ai:
        from ai_tagger import tagger_engine, preprocess_image_fast
        tagger_engine.ensure_model_loaded()
        tensor = preprocess_image_fast(full_path, 512)
        if tensor is not None:
            preds, ai_sug_folder = tagger_engine.predict_single(tensor, artist_thresh=0.75)
            conn2 = get_db()
            ai_links = []
            for tname, cat, conf in preds:
                tid = ensure_tag(conn2, tag_cache, tname, cat)
                if tid > 0:
                    ai_links.append((image_id, tid, round(conf, 4), "ai"))
            with conn2:
                if ai_links:
                    conn2.executemany(
                        "INSERT OR IGNORE INTO image_tags (image_id, tag_id, confidence, source) VALUES (?, ?, ?, ?)",
                        ai_links,
                    )
                conn2.execute(
                    "UPDATE images SET ai_tagged = 1, suggested_folder = COALESCE(?, suggested_folder) WHERE id = ?",
                    (ai_sug_folder, image_id),
                )
                refresh_tag_counts(conn2)
            conn2.close()
            return {
                "found": False,
                "method": "ai_content_fallback",
                "saucenao_hit": False,
                "iqdb_hit": False,
                "suggested_folder": ai_sug_folder,
                "tags_added": len(ai_links),
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
        delay_sec: float = 1.8,
        auto_move_to_artist_folder: bool = False,
    ) -> Dict[str, Any]:
        with self._lock:
            if self.running:
                return {"status": "already_running"}
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
            daemon=True,
        )
        self._thread.start()
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
            where = "WHERE media_type = 'image'"
            # Exclude images that ALREADY have a verified artist from pixiv/danbooru/saucenao/iqdb/gemini_mcp/folder
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

            for r in rows:
                if self.stop_requested:
                    break
                self.current_file = ""
                self.status_message = (
                    f"正在執行以圖搜圖與畫師校正 — 整體進度：{self.processed_count:,} / {self.total_target:,} 張"
                )

                res = reverse_search_and_tag_image(
                    image_id=r["id"],
                    min_similarity=min_similarity,
                    saucenao_api_key=saucenao_api_key,
                    fallback_to_ai=fallback_to_ai,
                    include_google_lens_url=False,
                )
                if res.get("danbooru_hit") or res.get("saucenao_hit") or res.get("iqdb_hit"):
                    self.hits_count += 1
                    self.calibrated_count += int(res.get("purged_wrong_ai_tags", 0))

                sug_folder = res.get("suggested_folder")
                if auto_move_to_artist_folder and sug_folder and r["folder"] == "" and res.get("artists"):
                    from app import move_images_to_folder
                    m_res = move_images_to_folder([r["id"]], sug_folder, auto_rename=True)
                    self.moved_count += m_res.get("moved", 0)

                self.processed_count += 1
                if not self.stop_requested and self.processed_count < self.total_target:
                    time.sleep(max(1.0, delay_sec))

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
        except Exception as e:
            self.status_message = f"❌ 以圖搜圖錯誤: {e}"
        finally:
            self.running = False


reverse_search_worker = BatchReverseSearchWorker()
