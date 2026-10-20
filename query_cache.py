"""Bounded single-flight caches invalidated by DB/WAL changes across processes."""
from collections import OrderedDict
from concurrent.futures import Future
import threading
import time
from pathlib import Path


def database_version():
    import database
    path = Path(database.DB_PATH)
    signature = [str(path.resolve())]
    for file in (path,Path(str(path)+"-wal")):
        try:
            stat = file.stat()
            signature.extend((stat.st_ino,stat.st_size,stat.st_mtime_ns))
        except FileNotFoundError:
            signature.extend((0,0,0))
    return tuple(signature)


class QueryCache:
    def __init__(self,capacity=8,ttl=30):
        self.capacity,self.ttl = capacity,ttl
        self._lock = threading.Lock()
        self._items = OrderedDict()
        self._pending = {}

    def get(self,key,load):
        key = (database_version(),key)
        with self._lock:
            entry = self._items.get(key)
            if entry and time.monotonic()-entry[0] < self.ttl:
                self._items.move_to_end(key)
                return entry[1]
            future = self._pending.get(key)
            owner = future is None
            if owner:
                future = self._pending[key] = Future()
        if not owner:
            return future.result(timeout=30)
        try:
            value = load()
            with self._lock:
                self._items[key] = (time.monotonic(),value)
                self._items.move_to_end(key)
                while len(self._items)>self.capacity:
                    self._items.popitem(last=False)
                self._pending.pop(key,None)
            future.set_result(value)
            return value
        except BaseException as error:
            with self._lock:
                self._pending.pop(key,None)
            future.set_exception(error)
            raise


natural_order = QueryCache(capacity=8)
recent_tags = QueryCache(capacity=16)


def newest_tag_rows(conn,limit):
    """Usually inspect a few newest images instead of every tag relationship.

    Stop only below the last selected timestamp, so timestamp ties are complete.
    A bounded fallback handles libraries with very few distinct tags or huge ties.
    """
    if limit<=0:
        return []
    found={};cursor=None
    for _ in range(4):
        params=[];where="missing_since IS NULL"
        if cursor is not None:
            where+=" AND (mtime<? OR (mtime=? AND id<?))"
            params=[cursor[0],cursor[0],cursor[1]]
        images=conn.execute(f"SELECT id,mtime FROM images WHERE {where} ORDER BY mtime DESC,id DESC LIMIT 128",params).fetchall()
        if not images:
            return sorted(found.values(),key=lambda row:(-row["latest_mtime"],row["id"]))[:limit]
        times={row["id"]:row["mtime"] for row in images}
        rows=conn.execute("SELECT it.image_id,t.id,t.name,t.category,t.zh_name,t.post_count FROM image_tags it JOIN tags t ON t.id=it.tag_id WHERE it.image_id IN ("+",".join("?" for _ in times)+") AND t.post_count>0",list(times)).fetchall()
        for row in rows:
            latest=times[row["image_id"]]
            previous=found.get(row["id"])
            if previous is None or latest>previous["latest_mtime"]:
                found[row["id"]]={**dict(row),"latest_mtime":latest}
        ranked=sorted(found.values(),key=lambda row:(-row["latest_mtime"],row["id"]))
        cursor=(images[-1]["mtime"],images[-1]["id"])
        if len(ranked)>=limit and cursor[0]<ranked[limit-1]["latest_mtime"]:
            return ranked[:limit]
        if len(images)<128:
            return ranked[:limit]
    return conn.execute("""SELECT t.id,t.name,t.category,t.zh_name,t.post_count,MAX(i.mtime) AS latest_mtime
        FROM tags t JOIN image_tags it ON t.id=it.tag_id JOIN images i ON i.id=it.image_id
        WHERE t.post_count>0 AND i.missing_since IS NULL GROUP BY t.id
        ORDER BY latest_mtime DESC,t.id ASC LIMIT ?""",(limit,)).fetchall()
