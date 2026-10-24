"""Evidence-backed publication dates. Filesystem dates are never publication dates."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlparse, urlunparse

import requests
import database
from background_work import FileLock, atomic_json, exclusive_job
from file_safety import gallery_path

TZ = dt.timezone(dt.timedelta(hours=8))
HEADERS = {"User-Agent": "TwitterTagGallery/1.0 (publication source lookup)", "Accept": "application/json"}
_history_cache = {}


def settings():
    path = database.DATA_DIR / "publication_settings.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return {"enabled": value.get("enabled") is True, "pad_digits": min(6, max(3, int(value.get("pad_digits", 4))))}
    except (OSError, ValueError, TypeError):
        return {"enabled": False, "pad_digits": 4}


def historical_name(image_id, fallback):
    """Old names are search hints only; they never prove a publication date."""
    root = str(database.DATA_DIR)
    if root not in _history_cache:
        names = {}
        for directory in (database.DATA_DIR / "file_journals", database.DATA_DIR / "rename_logs"):
            for path in sorted(directory.glob("*.json")):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    for entry in data.get("entries", data.get("items", [])):
                        name = entry.get("old_filename") or Path(entry.get("old_rel_path", "")).name
                        iid = entry.get("id")
                        if iid and name and not re.match(r"^20\d{6}_\d{2,6}(?:_|\.)", name):
                            names.setdefault(iid, name)
                except (OSError, ValueError, TypeError, AttributeError):
                    continue
        _history_cache[root] = names
    return _history_cache[root].get(image_id, fallback)


def original_page(url):
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port:
        raise ValueError("來源必須為 HTTPS 原作品頁面")
    if host in ("x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"):
        match = re.fullmatch(r"/([A-Za-z0-9_]+)/status/(\d+)(?:/photo/\d+)?/?", parsed.path)
        if not match or match[1].lower() in ("i", "intent", "home"):
            raise ValueError("需要包含作者帳號的原始貼文網址")
        return f"https://x.com/{match[1]}/status/{match[2]}"
    if host in ("www.pixiv.net", "pixiv.net"):
        match = re.fullmatch(r"/(?:[a-z]{2}/)?artworks/(\d+)/?", parsed.path)
        if match:
            return f"https://www.pixiv.net/artworks/{match[1]}"
    if host.endswith(".fanbox.cc") and re.fullmatch(r"/posts/\d+/?", parsed.path):
        return urlunparse(("https", host, parsed.path.rstrip("/"), "", "", ""))
    raise ValueError("目前自動日期查證支援 X、Pixiv 與 Fanbox 原作品頁；其他來源保留待查")


def _date(value):
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("來源日期沒有時區")
    timestamp = parsed.timestamp()
    if timestamp < dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc).timestamp() or timestamp > time.time() + 300:
        raise ValueError("來源日期不合理")
    return timestamp


def _image(image_id):
    conn = database.get_db()
    try:
        row = conn.execute("SELECT * FROM images WHERE id=? AND missing_since IS NULL", (image_id,)).fetchone()
        if not row:
            raise ValueError("圖片不存在或已標記遺失")
        return dict(row)
    finally:
        conn.close()


def fingerprint(image_id):
    row = _image(image_id)
    path = gallery_path(database.GALLERY_ROOT, row["rel_path"])
    stat = path.stat()
    version = f"{stat.st_size}:{stat.st_mtime_ns}"
    conn = database.get_db()
    try:
        old = conn.execute("SELECT * FROM publication_files WHERE image_id=?", (image_id,)).fetchone()
        if old and old["file_version"] == version and (not row.get("content_hash") or row["content_hash"] == old["content_hash"]):
            return {**row, **dict(old)}
        sha, md5 = hashlib.sha256(), hashlib.md5()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                sha.update(block); md5.update(block)
        after = path.stat()
        if (stat.st_size, stat.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("圖片正在修改，稍後再查")
        original = old["original_filename"] if old else historical_name(image_id, row["filename"])
        record = {"image_id": image_id, "original_filename": original, "file_version": version,
                  "content_hash": sha.hexdigest(), "md5": md5.hexdigest()}
        with conn:
            conn.execute("INSERT OR REPLACE INTO publication_files VALUES(?,?,?,?,?)", tuple(record.values()))
        return {**row, **record}
    finally:
        conn.close()


def sources(image_id):
    conn = database.get_db()
    try:
        return [dict(r) for r in conn.execute("SELECT s.* FROM publication_sources s JOIN publication_files f ON f.image_id=s.image_id AND f.content_hash=s.content_hash WHERE s.image_id=? ORDER BY s.published_at", (image_id,))]
    finally:
        conn.close()


def _source_info(url):
    # Danbooru's source endpoint has a separate 20/minute limit. Share pacing across processes.
    lock = FileLock(database.DATA_DIR / "locks" / "publication-http.lock")
    for _ in range(120):
        if lock.acquire():
            break
        time.sleep(.1)
    else:
        raise RuntimeError("來源查詢忙碌，請稍後重試")
    try:
        pacing = database.DATA_DIR / "publication-http.json"
        previous = json.loads(pacing.read_text()) if pacing.exists() else {}
        delay = min(3.1, max(0, previous.get("at", 0) + 3.1 - time.time()))
        if delay:
            time.sleep(delay)
        atomic_json(pacing, {"at": time.time()})
        response = requests.get("https://danbooru.donmai.us/source.json", params={"url": url}, headers=HEADERS, timeout=15)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("來源解析資料格式無效")
        return result
    finally:
        lock.release()


def _record(image_id, url, info, file, evidence):
    page = original_page(url)
    if info.get("page_url") and original_page(info["page_url"]) != page:
        raise ValueError("來源解析回傳了不同作品")
    published = info.get("published_at")
    if not published:
        raise ValueError("原作品沒有可確認的發布日期")
    timestamp = _date(published)
    profiles = info.get("artist", {}).get("profile_urls", [])
    if not profiles:
        raise ValueError("原來源缺少作者資料")
    if evidence.get("method") == "danbooru_md5":
        source_artists = {a.get("name") for a in info.get("artist", {}).get("artists", []) if isinstance(a, dict)}
        if not source_artists.intersection(evidence.get("artist_tags", [])):
            raise ValueError("來源帳號尚未與作品畫師對上，需由模型核對原作者")
    # Bind the metadata to the exact content observed before the network lookup.
    if fingerprint(image_id)["content_hash"] != file["content_hash"]:
        raise ValueError("查證期間圖片已變更")
    conn = database.get_db()
    try:
        with conn:
            conn.execute("INSERT OR REPLACE INTO publication_sources VALUES(?,?,?,?,?,?)", (image_id, page, timestamp, file["content_hash"], json.dumps({**evidence, "author_profiles": profiles}, ensure_ascii=False), time.time()))
    finally:
        conn.close()
    return {"source_url": page, "published_at": timestamp, "date": dt.datetime.fromtimestamp(timestamp, TZ).strftime("%Y%m%d")}


def _attempt(image_id, status, error=""):
    conn = database.get_db()
    try:
        with conn:
            conn.execute("INSERT OR REPLACE INTO publication_attempts VALUES(?,?,?,?,?)", (image_id, status, str(error)[:1000], time.time() + (300 if status == "failed" else 86400), time.time()))
    finally:
        conn.close()


def trace(job_id, image_ids):
    import agent_jobs
    agent_jobs.require_running(job_id, "publication")
    with agent_jobs.connection() as conn:
        allowed = {r[0] for r in conn.execute("SELECT image_id FROM agent_items WHERE job_id=?", (job_id,))}
    if not set(image_ids) <= allowed or len(image_ids) > 50:
        raise ValueError("圖片不在工作範圍，或超過每批 50 張")
    files, results = {}, []
    for iid in image_ids:
        agent_jobs.require_running(job_id)
        try:
            file = fingerprint(iid)
            if not file.get("pixiv_id"):
                match = re.search(r"(\d{7,10})_p\d+", file["original_filename"])
                if match:
                    file["pixiv_id"] = match[1]
            cached = sources(iid)
            if cached:
                agent_jobs.update_item(job_id, iid, "verified")
                results.append({"image_id": iid, "status": "verified", "sources": cached, "cached": True})
                continue
            files.setdefault(file["md5"], []).append(file)
        except (OSError, ValueError) as exc:
            agent_jobs.update_item(job_id, iid, "failed", str(exc))
            results.append({"image_id": iid, "status": "failed", "error": str(exc)})
    if not files:
        return {"results": results}
    try:
        response = requests.get("https://danbooru.donmai.us/posts.json", params={"tags": "md5:" + ",".join(files), "limit": 100}, headers=HEADERS, timeout=15)
        response.raise_for_status()
        posts = response.json()
        if not isinstance(posts, list):
            raise ValueError("Danbooru 查詢資料格式錯誤")
    except (requests.RequestException, ValueError) as exc:
        for group in files.values():
            for file in group:
                agent_jobs.require_running(job_id)
                _attempt(file["id"], "failed", str(exc))
                agent_jobs.update_item(job_id, file["id"], "failed", str(exc))
                results.append({"image_id": file["id"], "status": "failed", "error": str(exc), "original_filename": file["original_filename"], "pixiv_id": file.get("pixiv_id")})
        return {"results": results}
    pixiv_ids = {str(f.get("pixiv_id") or (re.search(r"(\d{7,10})_p\d+", f["original_filename"])[1] if re.search(r"(\d{7,10})_p\d+", f["original_filename"]) else "")) for group in files.values() for f in group}
    pixiv_ids.discard("")
    pixiv_posts = []
    if pixiv_ids:
        try:
            response = requests.get("https://danbooru.donmai.us/posts.json", params={"tags": "pixiv:" + ",".join(sorted(pixiv_ids)), "limit": 100}, headers=HEADERS, timeout=15)
            response.raise_for_status()
            pixiv_posts = response.json()
            if not isinstance(pixiv_posts, list):
                pixiv_posts = []
        except (requests.RequestException, ValueError):
            pass
    by_hash = {}
    for post in posts:
        if isinstance(post, dict):
            by_hash.setdefault(post.get("md5"), []).append(post)
    for digest, group in files.items():
        for file in group:
            iid = file["id"]
            hints = [post.get("source", "") for post in pixiv_posts if isinstance(post, dict) and str(post.get("pixiv_id") or "") == str(file.get("pixiv_id") or "") and post.get("source")]
            errors = []
            for post in by_hash.get(digest, []):
                agent_jobs.require_running(job_id)
                try:
                    url = original_page(post.get("source", ""))
                    info = _source_info(url)
                    agent_jobs.require_running(job_id)
                    _record(iid, url, info, file, {"method": "danbooru_md5", "post_id": post["id"], "md5": digest, "artist_tags": (post.get("tag_string_artist") or "").split()})
                    hints.append(url)
                except (ValueError, requests.RequestException, RuntimeError) as exc:
                    errors.append(str(exc))
            agent_jobs.require_running(job_id)
            known = sources(iid)
            status = "verified" if known else "review"
            _attempt(iid, "verified" if known else "unknown", "; ".join(errors))
            agent_jobs.update_item(job_id, iid, status)
            results.append({"image_id": iid, "status": status, "original_filename": file["original_filename"], "pixiv_id": file.get("pixiv_id"), "sources": known, "candidates": hints, "errors": errors})
    return {"results": results}


def submit(job_id, image_id, source_url, image_url, author_profile_url, match_reason):
    import agent_jobs
    agent_jobs.require_running(job_id, "publication")
    with agent_jobs.connection() as conn:
        if not conn.execute("SELECT 1 FROM agent_items WHERE job_id=? AND image_id=?", (job_id, image_id)).fetchone():
            raise ValueError("圖片不在工作範圍")
    file = fingerprint(image_id)
    url = original_page(source_url)
    info = _source_info(url)
    if image_url not in info.get("image_urls", []) or author_profile_url not in info.get("artist", {}).get("profile_urls", []):
        raise ValueError("提交的圖片或作者網址不屬於原作品，保留待查")
    if len(match_reason.strip()) < 20:
        raise ValueError("請提供至少 20 字的圖片、頁碼及作者核對依據")
    agent_jobs.require_running(job_id)
    result = _record(image_id, url, info, file, {"method": "antigravity_visual_verified", "image_url": image_url, "author_profile_url": author_profile_url, "match_reason": match_reason, "job_id": job_id})
    agent_jobs.update_item(job_id, image_id, "verified")
    _attempt(image_id, "verified")
    return result


def plan(folder, pad_digits=4):
    conn = database.get_db()
    try:
        rows = [dict(r) for r in conn.execute("""SELECT i.*,f.original_filename,f.file_version,f.content_hash AS verified_hash,
          MIN(s.published_at) AS published_at FROM images i JOIN publication_files f ON f.image_id=i.id
          JOIN publication_sources s ON s.image_id=i.id AND s.content_hash=f.content_hash
          WHERE i.folder=? AND i.missing_since IS NULL GROUP BY i.id""", (folder,))]
    finally:
        conn.close()
    from renamer import win_natural_key
    usable = []
    for row in rows:
        current = fingerprint(row["id"])
        if current["content_hash"] == row["verified_hash"]:
            usable.append(row)
    def order(row):
        page = re.search(r"_p(\d+)", row["original_filename"])
        return (row["published_at"], int(page[1]) if page else -1, win_natural_key(row["original_filename"]), row["id"])
    usable.sort(key=order)
    width = max(pad_digits, len(str(len(usable))))
    entries = []
    for index, row in enumerate(usable, 1):
        date = dt.datetime.fromtimestamp(row["published_at"], TZ).strftime("%Y%m%d")
        name = f"{date}_{index:0{width}d}{row['ext']}"
        target = (Path(row["rel_path"]).parent / name).as_posix()
        if target != row["rel_path"]:
            entries.append({"id": row["id"], "old_rel_path": row["rel_path"], "new_rel_path": target})
    return {"folder": folder, "entries": entries, "verified": len(usable)}


@exclusive_job("發布日期自動改名")
def rename_folder(folder, pad_digits=4, job_id=None):
    from file_operations import execute
    planned = plan(folder, pad_digits)
    # execute rejects occupied destinations and journals swaps; no _dup suffixes.
    if not planned["entries"]:
        return {"renamed": 0}
    def check_job(conn, entries):
        if job_id:
            import agent_jobs
            agent_jobs.require_running(job_id, "publication")
    result = execute(database.GALLERY_ROOT, database.DATA_DIR, database.get_db, planned["entries"], kind="rename", after_write=check_job)
    return {"renamed": len(planned["entries"]), **result}


def complete_batch(job_id, image_ids):
    import agent_jobs
    job = agent_jobs.require_running(job_id, "publication")
    if len(image_ids) > 50:
        raise ValueError("每批最多 50 張")
    with agent_jobs.connection() as conn:
        allowed = {r[0] for r in conn.execute("SELECT image_id FROM agent_items WHERE job_id=? AND status IN ('review','verified','pending','failed')", (job_id,))}
    if not set(image_ids) <= allowed:
        raise ValueError("包含非本工作圖片，或本批已完成")
    folders, errors, renamed = set(), [], 0
    for iid in image_ids:
        agent_jobs.require_running(job_id)
        row = _image(iid)
        if sources(iid):
            folders.add(row["folder"])
        else:
            with agent_jobs.connection() as conn:
                previous = conn.execute("SELECT status FROM agent_items WHERE job_id=? AND image_id=?", (job_id, iid)).fetchone()
            if previous[0] != "failed":
                agent_jobs.update_item(job_id, iid, "unknown")
    for folder in sorted(folders):
        agent_jobs.require_running(job_id)
        try:
            renamed += rename_folder(folder, job["context"].get("pad_digits", 4), job_id)["renamed"]
        except (OSError, ValueError, RuntimeError) as exc:
            errors.append({"folder": folder, "error": str(exc)})
    for iid in image_ids:
        if sources(iid):
            failed = any(e["folder"] == _image(iid)["folder"] for e in errors)
            agent_jobs.update_item(job_id, iid, "failed" if failed else "done", "改名失敗，請檢查資料夾衝突" if failed else "")
    with agent_jobs.connection() as conn:
        remaining = conn.execute("SELECT COUNT(*) FROM agent_items WHERE job_id=? AND status IN ('pending','review','verified')", (job_id,)).fetchone()[0]
    result = {"renamed": renamed, "errors": errors, "remaining": remaining}
    if remaining == 0:
        agent_jobs.finish(job_id, result)
    return result


def overview(folder="__ALL_ARTIST_FOLDERS__"):
    conn = database.get_db()
    try:
        rows = conn.execute("""SELECT i.id,i.folder,i.filename,i.rel_path,f.file_version,
          MIN(s.published_at) AS published_at FROM images i LEFT JOIN publication_files f ON f.image_id=i.id
          LEFT JOIN publication_sources s ON s.image_id=i.id AND s.content_hash=f.content_hash
          WHERE i.missing_since IS NULL GROUP BY i.id ORDER BY i.folder,i.id""").fetchall()
    finally:
        conn.close()
    items = []
    for row in rows:
        if folder == "__ALL_ARTIST_FOLDERS__" and (not row["folder"] or row["folder"] in database.NON_ARTIST_FOLDERS):
            continue
        if folder not in ("__ALL__", "__ALL_ARTIST_FOLDERS__") and row["folder"] != ("" if folder == "__ROOT__" else folder):
            continue
        item = dict(row)
        if item["published_at"]:
            stat = gallery_path(database.GALLERY_ROOT, item["rel_path"]).stat() if gallery_path(database.GALLERY_ROOT, item["rel_path"]).exists() else None
            if stat is None or item["file_version"] != f"{stat.st_size}:{stat.st_mtime_ns}":
                item["published_at"] = None
        items.append(item)
    return {"total": len(items), "verified": sum(bool(i["published_at"]) for i in items),
            "pending": sum(not i["published_at"] for i in items), "items": items[:100]}
