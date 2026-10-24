"""Persisted, evidence-based artist identities. Network calls never hold a DB transaction."""
import hashlib
import json
import re
import sqlite3
import time
import unicodedata
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests

import database
from background_work import FileLock, background_checkpoint
from query_cache import QueryCache, invalidate_gallery_queries

_index = QueryCache(capacity=2)


def name_key(name):
    name = unicodedata.normalize("NFC", str(name)).strip()
    if name.startswith("twitter:@"):
        name = name[len("twitter:@"):]
    return re.sub(r"\s+", "_", name.lstrip("@")).casefold()


def profile_key(url):
    """Account keys, never display names or artwork/post IDs."""
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("https", "http") or parts.username or parts.password or parts.port:
            return None
        host = (parts.hostname or "").lower().removeprefix("www.")
        path = parts.path.rstrip("/")
        if host in ("x.com", "twitter.com"):
            user_id = parse_qs(parts.query).get("user_id", [""])[0]
            if path == "/intent/user" and user_id.isdigit():
                return ("twitter_id", str(int(user_id)))
            match = re.fullmatch(r"/(?:i/user|intent/user)/(\d+)", path)
            if match:
                return ("twitter_id", match[1])
            match = re.fullmatch(r"/([A-Za-z0-9_]{1,15})(?:/status/\d+)?", path)
            if match and match[1].lower() not in {"i", "home", "search", "intent", "share", "explore", "settings"}:
                return ("twitter", match[1].lower())
        if host == "pixiv.net":
            user_id = parse_qs(parts.query).get("id", [""])[0]
            if path == "/member.php" and user_id.isdigit():
                return ("pixiv", str(int(user_id)))
            match = re.fullmatch(r"/(?:[a-z]{2}/)?(?:users|fanbox/creator)/(\d+)", path)
            if match:
                return ("pixiv", str(int(match[1])))
        if host == "fanbox.cc":
            match = re.fullmatch(r"/@([A-Za-z0-9_-]+)(?:/posts/\d+)?", path)
            if match:
                return ("fanbox", match[1].lower())
        if host.endswith(".fanbox.cc") and host.count(".") == 2:
            if re.fullmatch(r"[a-z0-9_-]+", host.split(".")[0]):
                return ("fanbox", host.split(".")[0])
    except (TypeError, ValueError):
        pass
    return None


def _keys(name, urls):
    keys = {key for url in urls if (key := profile_key(url))}
    if name_key(name):
        keys.add(("name", name_key(name)))
    if str(name).strip().startswith(("@", "twitter:@")):
        keys.add(("twitter", name_key(name)))
    return keys


def identity_index(conn):
    rows = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM artist_identities")}
    keys = {}
    for row in conn.execute("SELECT * FROM artist_identity_keys"):
        keys.setdefault((row["kind"], row["value"]), set()).add(row["artist_id"])
    return rows, keys


def _read_index():
    # Reading translations must not create a gallery/database or trigger migrations.
    if not Path(database.DB_PATH).is_file():
        return {}, {}
    conn = sqlite3.connect(Path(database.DB_PATH).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return identity_index(conn)
    except sqlite3.OperationalError as error:
        if "no such table" in str(error):
            return {}, {}
        raise
    finally:
        conn.close()


def lookup(name="", profile_urls=(), *, conn=None):
    rows, keys = identity_index(conn) if conn is not None else _index.get("identities", _read_index)
    query = _keys(name, profile_urls)
    account_queries = [k for k in query if k[0] != "name"]
    if account_queries and any(not keys.get(k) for k in account_queries):
        return None
    accounts = set().union(*(keys.get(k, set()) for k in query if k[0] != "name"))
    if accounts:
        return rows[next(iter(accounts))] if len(accounts) == 1 else None
    if any(key[0] != "name" for key in query):
        return None
    matches = keys.get(("name", name_key(name)), set())
    return rows[next(iter(matches))] if len(matches) == 1 else None


def known_folder(name, *, conn=None):
    row = lookup(name, conn=conn)
    return row["folder_name"] if row else None


def _choose_folder(conn, canonical_name, aliases, account_keys):
    from zh_dict import DANBOORU_ARTIST_TO_FOLDER
    from gallery_service import artist_directory
    names = {name_key(n) for n in [canonical_name, *aliases]}
    names.update(value for kind, value in account_keys if kind in ("twitter", "fanbox"))
    mapped = {DANBOORU_ARTIST_TO_FOLDER[n] for n in names if n in DANBOORU_ARTIST_TO_FOLDER}
    names.update(name_key(n) for n in mapped)
    existing = {r["folder"]: r["n"] for r in conn.execute(
        "SELECT folder,COUNT(*) n FROM images WHERE folder!='' AND missing_since IS NULL GROUP BY folder")}
    if database.GALLERY_ROOT.is_dir():
        for folder in database.GALLERY_ROOT.iterdir():
            if folder.is_dir() and not folder.name.startswith("."):
                existing.setdefault(folder.name, 0)
    candidates = [f for f in existing if f not in database.NON_ARTIST_FOLDERS and name_key(f) in names]
    if candidates:
        folder = sorted(candidates, key=lambda f: (-existing[f], f.casefold(), f))[0]
    else:
        folder = sorted(mapped)[0] if mapped else re.sub(r'[<>:"/\\|?*]', "_", canonical_name.replace("_", " ")).strip(" .")
    folder = artist_directory(database.GALLERY_ROOT, folder)[1]
    if not folder or folder in database.NON_ARTIST_FOLDERS:
        raise ValueError("畫師歸檔資料夾不可為根目錄或系統分類")
    return folder


def remember(canonical_name, aliases, profile_urls, *, source, evidence, danbooru_id=None):
    if not canonical_name.strip() or len(canonical_name) > 200:
        raise ValueError("畫師名稱無效")
    accounts = {key for url in profile_urls if (key := profile_key(url))}
    if not accounts and danbooru_id is None:
        raise ValueError("需要可確認的畫師帳號")
    aliases = list(dict.fromkeys([canonical_name, *aliases]))
    if len(aliases) > 100 or any(not isinstance(n, str) or not n.strip() or len(n) > 300 for n in aliases):
        raise ValueError("畫師別名無效或過多")
    keys = accounts | {("name", name_key(n)) for n in aliases}
    keys |= {("name", value) for kind, value in accounts if kind in ("twitter", "fanbox")}
    if danbooru_id is not None:
        keys.add(("danbooru", str(danbooru_id)))
    conn = database.get_db()
    try:
        def write(c):
            c.execute("BEGIN IMMEDIATE")
            rows, index = identity_index(c)
            matched = set().union(*(index.get(key, set()) for key in keys if key[0] != "name"))
            if len(matched) > 1:
                raise ValueError("帳號對應到不同的既有畫師，保留待查，未合併身分")
            previous = rows[next(iter(matched))] if matched else None
            if previous and danbooru_id and previous["danbooru_id"] not in (None, danbooru_id):
                raise ValueError("Danbooru 畫師 ID 衝突，未合併身分")
            if previous:
                artist_id, folder = previous["id"], previous["folder_name"]
                old = json.loads(previous["evidence_json"])
                proofs = (old + [{"source": source, "evidence": evidence}])[-30:]
                c.execute("UPDATE artist_identities SET danbooru_id=COALESCE(danbooru_id,?),evidence_json=?,updated_at=? WHERE id=?",
                          (danbooru_id, json.dumps(proofs, ensure_ascii=False), time.time(), artist_id))
            else:
                folder = _choose_folder(c, canonical_name, aliases, accounts)
                # A common display name is not proof that two artists are the same.
                if c.execute("SELECT 1 FROM artist_identities WHERE folder_name=?", (folder,)).fetchone():
                    suffix = str(danbooru_id or hashlib.sha256(json.dumps(sorted(accounts)).encode()).hexdigest()[:8])
                    folder = f"{folder[:170]} ({suffix})"
                artist_id = c.execute("INSERT INTO artist_identities(canonical_name,folder_name,danbooru_id,source,evidence_json,updated_at) VALUES(?,?,?,?,?,?) RETURNING id",
                    (canonical_name, folder, danbooru_id, source, json.dumps([{"source": source, "evidence": evidence}], ensure_ascii=False), time.time())).fetchone()[0]
            keys.add(("name", name_key(folder)))
            c.executemany("INSERT OR IGNORE INTO artist_identity_keys(kind,value,artist_id) VALUES(?,?,?)",
                          [(kind, value, artist_id) for kind, value in keys])
            return dict(c.execute("SELECT * FROM artist_identities WHERE id=?", (artist_id,)).fetchone())
        row = database.run_db_write_with_retry(conn, write)
    finally:
        conn.close()
    _index.clear()
    invalidate_gallery_queries()
    return row


def _danbooru(name, urls):
    """Use fresh API results; old JSON caches may contain permanent negative hits."""
    queries = [{"search[url_matches]": u} for u in urls if profile_key(u)]
    if name_key(name):
        queries.append({"search[any_name_matches]": name.strip().lstrip("@")})
    for query in queries:
        response = requests.get("https://danbooru.donmai.us/artists.json",
            params={**query, "only": "id,name,other_names,urls,is_deleted", "limit": 20},
            headers={"User-Agent": "TwitterTagGallery/1.0 (artist identity lookup)"}, timeout=8)
        response.raise_for_status()
        entries = response.json()
        if not isinstance(entries, list):
            raise ValueError("Danbooru 畫師資料格式無效")
        matches = []
        for item in entries:
            if not isinstance(item, dict) or not isinstance(item.get("other_names", []), list):
                continue
            if item.get("is_deleted") or not item.get("id") or not item.get("name"):
                continue
            item_urls = [u["url"] for u in item.get("urls", []) if isinstance(u, dict) and isinstance(u.get("url"), str) and u.get("is_active", True)]
            names = [item["name"], *item.get("other_names", [])]
            query_accounts = {profile_key(u) for u in urls if profile_key(u)}
            account_match = bool(query_accounts) and query_accounts.issubset({profile_key(u) for u in item_urls if profile_key(u)})
            if account_match or (not urls and name_key(name) in {name_key(n) for n in names}):
                matches.append((item, item_urls))
        if len(matches) > 1:
            raise ValueError("Danbooru 回傳多個畫師，不能自動選第一筆")
        if matches:
            item, item_urls = matches[0]
            return remember(item["name"], [name, *item.get("other_names", [])] if name else item.get("other_names", []),
                item_urls, source="danbooru", evidence={"artist_url": f"https://danbooru.donmai.us/artists/{item['id']}"}, danbooru_id=int(item["id"]))
    return None


def resolve(artist_name="", profile_urls=(), *, force_refresh=False):
    urls = list(dict.fromkeys(u for u in profile_urls if profile_key(u)))
    if artist_name.strip().startswith(("@", "twitter:@")):
        handle_url = f"https://x.com/{name_key(artist_name)}"
        if profile_key(handle_url) and handle_url not in urls:
            urls.append(handle_url)
    if not name_key(artist_name) and not urls:
        raise ValueError("需要畫師名稱或平台網址")
    found = lookup(artist_name, urls)
    if found:
        if urls and artist_name and known_folder(artist_name) != found["folder_name"]:
            found = remember(found["canonical_name"], [artist_name], urls, source="platform_profile",
                             evidence={"profile_urls": urls}, danbooru_id=found["danbooru_id"])
        return {"status": "resolved", **found}
    key = hashlib.sha256(json.dumps([name_key(artist_name), sorted(_keys("", urls))]).encode()).hexdigest()
    lock = FileLock(database.DATA_DIR / "locks" / f"artist-{key}.lock")
    if not lock.acquire():
        return {"status": "pending", "request_key": key, "folder_name": None}
    try:
        conn = database.get_db()
        try:
            previous = conn.execute("SELECT * FROM artist_identity_requests WHERE request_key=?", (key,)).fetchone()
            if previous and previous["retry_at"] > time.time() and not force_refresh:
                return {**dict(previous), "folder_name": None}
            with conn:
                conn.execute("INSERT INTO artist_identity_requests(request_key,artist_name,profile_urls_json,status,updated_at) VALUES(?,?,?,'pending',?) ON CONFLICT(request_key) DO UPDATE SET attempts=attempts+1,status='pending',updated_at=excluded.updated_at",
                             (key, artist_name, json.dumps(urls), time.time()))
        finally:
            conn.close()
        status, error, found = "pending_model", "", None
        try:
            background_checkpoint()
            found = _danbooru(artist_name, urls)
        except (requests.RequestException, ValueError) as exc:
            error = str(exc)[:500]
        status = "resolved" if found else status
        conn = database.get_db()
        try:
            with conn:
                conn.execute("UPDATE artist_identity_requests SET status=?,last_error=?,retry_at=?,updated_at=? WHERE request_key=?",
                             (status, error, time.time() + (300 if status == "retry" else 86400), time.time(), key))
        finally:
            conn.close()
        agent_job_id = None
        if not found:
            import agent_jobs
            if agent_jobs.bridge_token():
                agent_job_id = agent_jobs.create("artist_identity", {"artist_name": artist_name, "profile_urls": urls})["id"]
        return {"status": status, "request_key": key, **(found or {"folder_name": None}), "last_error": error, "agent_job_id": agent_job_id,
                "next_step": None if found else "由 MCP 客戶端搜尋作者頁面，查證後呼叫 gallery_confirm_artist_identity；圖庫不啟動模型程序"}
    finally:
        lock.release()


def confirm_model(canonical_name, aliases, profile_urls, evidence, *, query_name="", query_urls=(), source="model_mcp"):
    accounts = {profile_key(u) for u in profile_urls}
    if None in accounts or len(accounts) < 2 or len({k[0] for k in accounts}) < 2:
        raise ValueError("模型需要至少兩個不同平台的有效畫師帳號")
    edges = []
    for item in evidence:
        a, b = profile_key(item["source_url"]), profile_key(item["linked_url"])
        if a not in accounts or b not in accounts or a == b:
            raise ValueError("查證依據必須是作者頁面連到另一個提交的帳號")
        edges.append((a, b))
    reached = {next(iter(accounts))}
    for _ in accounts:
        for a, b in edges:
            if a in reached or b in reached:
                reached.update((a, b))
    if reached != accounts:
        raise ValueError("所有平台帳號都必須有可追溯的連結依據")
    if query_urls and not {profile_key(u) for u in query_urls}.issubset(accounts):
        raise ValueError("模型結果沒有涵蓋原本待查的帳號")
    if query_name and name_key(query_name) not in {name_key(n) for n in [canonical_name, *aliases]} | {v for _, v in accounts}:
        raise ValueError("模型結果沒有涵蓋原本待查的名稱")
    return remember(canonical_name, aliases, profile_urls, source=source, evidence=evidence)


def confirm_request(request_key, canonical_name, aliases, profile_urls, evidence):
    """MCP clients submit evidence for a queued request, never arbitrary unrelated identities."""
    conn = database.get_db()
    try:
        request = conn.execute("SELECT * FROM artist_identity_requests WHERE request_key=?", (request_key,)).fetchone()
        if request is None:
            raise ValueError("找不到待查身分；請先呼叫 gallery_resolve_artist_identity")
        request = dict(request)
    finally:
        conn.close()
    row = confirm_model(canonical_name, aliases, profile_urls, evidence,
                        query_name=request["artist_name"], query_urls=json.loads(request["profile_urls_json"]))
    conn = database.get_db()
    try:
        with conn:
            conn.execute("UPDATE artist_identity_requests SET status='resolved',last_error='',retry_at=0,updated_at=? WHERE request_key=?",
                         (time.time(), request_key))
    finally:
        conn.close()
    return {"status": "resolved", "request_key": request_key, **row}


def resolve_image_folders(image_ids):
    """Resolve primary attribution per image; co-occurrence never proves aliases."""
    ids = list(dict.fromkeys(image_ids))
    conn = database.get_db()
    rows = []
    try:
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            rows.extend(conn.execute("SELECT it.image_id,t.name,it.confidence,it.source FROM image_tags it JOIN tags t ON t.id=it.tag_id "
                "WHERE t.category='artist' AND it.image_id IN (" + ",".join("?" for _ in chunk) + ") ORDER BY it.confidence DESC,t.id", chunk).fetchall())
    finally:
        conn.close()
    rank = {"manual": 0, "gemini_mcp": 1, "danbooru": 2, "pixiv": 3, "saucenao": 4, "twitter": 5, "iqdb": 6, "folder": 7, "ai": 8}
    primary = {}
    for row in sorted(rows, key=lambda r: rank.get(r["source"], 9)):
        if row["confidence"] >= (0.75 if row["source"] == "ai" else 0.2):
            primary.setdefault(row["image_id"], row["name"])
    resolved, pending, seen = {}, [], {}
    for iid, name in primary.items():
        background_checkpoint()
        if name not in seen:
            seen[name] = resolve(name)
        result = seen[name]
        if result.get("folder_name"):
            resolved[iid] = result["folder_name"]
        else:
            pending.append(iid)
    conn = database.get_db()
    try:
        with conn:
            conn.executemany("UPDATE images SET suggested_folder=? WHERE id=?", [(folder, iid) for iid, folder in resolved.items()])
            conn.executemany("UPDATE images SET suggested_folder=NULL WHERE id=?", [(iid,) for iid in pending])
    finally:
        conn.close()
    return {"folders": resolved, "pending_image_ids": pending}


def status(limit=20):
    conn = database.get_db()
    try:
        queued = [dict(r) for r in conn.execute("SELECT * FROM artist_identity_requests WHERE status!='resolved' ORDER BY updated_at DESC LIMIT ?", (limit,))]
        for row in queued:
            row["profile_urls"] = json.loads(row["profile_urls_json"])
        return {"identities": conn.execute("SELECT COUNT(*) FROM artist_identities").fetchone()[0],
                "pending_count": conn.execute("SELECT COUNT(*) FROM artist_identity_requests WHERE status!='resolved'").fetchone()[0],
                "requests": queued, "mode": "mcp",
                "instructions": "由目前 MCP 客戶端搜尋並閱讀作者頁面。僅接受作者頁面明確連到其他平台帳號的依據；名字或畫風相似不構成確認。查證後以 request_key 與全部帳號、別名及 source_url/linked_url 呼叫 gallery_confirm_artist_identity。無依據就保留待查；不要啟動 CLI 或自行搬檔。"}
    finally:
        conn.close()
