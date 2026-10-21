"""Read-only artist identities shared by sidebar tags and image filtering."""
from query_cache import artist_tags, recent_tags, newest_tag_rows
from zh_dict import get_tag_display_name


def artist_index(conn):
    def load():
        groups = {}
        for row in conn.execute("SELECT id,name,category,zh_name,post_count FROM tags WHERE category='artist' ORDER BY id"):
            label = get_tag_display_name(row["name"], "artist", row["zh_name"])
            key = label.casefold()
            if key not in groups:
                groups[key] = {**dict(row), "zh_name": label, "aliases": [], "tag_ids": []}
            group = groups[key]
            group["aliases"].append(row["name"])
            group["tag_ids"].append(row["id"])
            group["post_count"] = max(group["post_count"], row["post_count"])
        return {"rows": list(groups.values()),
                "by_name": {name: group for group in groups.values() for name in group["aliases"]}}
    return artist_tags.get("identities", load)


def artist_filter_names(conn, name):
    group = artist_index(conn)["by_name"].get(name)
    return group["aliases"] if group else [name]


def artist_rows(conn):
    def load():
        result = []
        for group in artist_index(conn)["rows"]:
            if group["post_count"] <= 0:
                continue
            row = {key: value for key, value in group.items() if key != "tag_ids"}
            if len(group["tag_ids"]) > 1:
                placeholders = ",".join("?" for _ in group["tag_ids"])
                row["post_count"] = conn.execute(f"""SELECT COUNT(DISTINCT it.image_id)
                    FROM image_tags it JOIN images i ON i.id=it.image_id
                    WHERE it.tag_id IN ({placeholders}) AND i.missing_since IS NULL""", group["tag_ids"]).fetchone()[0]
            if row["post_count"] > 0:
                result.append(row)
        return result
    return artist_tags.get("sidebar", load)


def matches_artist(row, query):
    return not query or any(query in name.casefold() for name in [row["zh_name"], *row["aliases"]])


def sidebar_tag_rows(conn, category, q, limit):
    if limit <= 0:
        return []
    params = []
    where = ["post_count > 0", "category != 'artist'"]
    if category and category != "all":
        where.append("category = ?")
        params.append(category)
    if q.strip():
        where.append("(name LIKE ? OR zh_name LIKE ?)")
        params.extend([f"%{q.strip()}%"] * 2)
    rows = [] if category == "artist" else list(conn.execute(f"""
        SELECT id,name,category,zh_name,post_count FROM tags
        WHERE {' AND '.join(where)} ORDER BY post_count DESC,name ASC LIMIT ?
    """, [*params, limit]).fetchall())
    if category in ("", "all", "artist"):
        rows.extend(row for row in artist_rows(conn) if matches_artist(row, q.strip().casefold()))
    return sorted(rows, key=lambda row: (-row["post_count"], row["name"]))[:limit]


def recent_sidebar_tag_rows(conn, q, limit):
    def load():
        by_name = {name: row for row in artist_rows(conn) for name in row["aliases"]}
        query = q.strip().casefold()
        def transform(row):
            if row["category"] == "artist":
                group = by_name.get(row["name"])
                return group if group and matches_artist(group, query) else None
            label = get_tag_display_name(row["name"], row["category"], row["zh_name"])
            return row if not query or query in row["name"].casefold() or query in label.casefold() else None
        return newest_tag_rows(conn, limit, transform=transform)
    return recent_tags.get(("sidebar", q, limit), load)
