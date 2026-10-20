# -*- coding: utf-8 -*-
"""
標籤相關性分析與 Gemini Flash 語意處理引擎 (semantic_engine.py)
功能：
1. 計算圖庫內所有標籤的共現率 (Co-occurrence)、Jaccard 相似度與互資訊 (PMI)，
   找出「某畫師最常畫的角色/服裝」、「某標籤最常搭配的相關標籤」。
2. 支援透過 Gemini Flash (google-generativeai / Antigravity SDK) 進行：
   - 自然語言意圖展開為多重標籤組合搜尋 (例如「白髮紅瞳的兔女郎或女僕」 -> 自動推理出對應 Danbooru 標籤組合)
   - 自動為未翻譯的英文/日文標籤生成精準的繁體中文翻譯並寫回資料庫
   - 將零散的視覺標籤歸納為高階「主題合成標籤」(例如：黑絲美腿、泳裝特輯、女僕裝、蔚藍檔案等)
"""

import json
import math
import os
from collections import defaultdict
from typing import List, Dict, Any, Optional, Tuple

from database import get_db, ensure_tag, refresh_tag_counts
from zh_dict import TAG_ZH_DICT, get_tag_zh

# 內建高階語意主題叢集規則 (即使離線或透過 MCP 皆可秒級聚合相關性標籤)
SEMANTIC_THEME_CLUSTERS = {
    "主題:黑絲與美腿": {
        "core_tags": ["black_pantyhose", "pantyhose", "black_thighhighs", "torn_pantyhose", "kneehighs", "garter_straps", "fishnets", "zettai_ryouiki"],
        "related_tags": ["thighs", "thick_thighs", "bare_legs", "high_heels", "miniskirt", "pencil_skirt", "office_lady"],
        "description": "包含黑絲連褲襪、過膝襪、吊帶襪、網襪、破洞絲襪與絕對領域等腿部屬性"
    },
    "主題:白絲與純慾": {
        "core_tags": ["white_pantyhose", "white_thighhighs", "striped_thighhighs"],
        "related_tags": ["thighs", "zettai_ryouiki", "pleated_skirt", "ribbon"],
        "description": "包含白絲連褲襪、白過膝襪等清純與反差感穿搭"
    },
    "主題:兔女郎與緊身衣": {
        "core_tags": ["bunny_girl", "playboy_bunny", "leotard", "bodysuit", "rabbit_ears"],
        "related_tags": ["pantyhose", "fishnets", "high_heels", "cleavage", "wrist_cuffs", "bowtie"],
        "description": "包含兔女郎裝、連體緊身衣、網襪與兔耳造型"
    },
    "主題:泳裝與夏日海灘": {
        "core_tags": ["swimsuit", "bikini", "micro_bikini", "school_swimsuit", "one-piece_swimsuit"],
        "related_tags": ["wet", "sweat", "cleavage", "sideboob", "underboob", "navel", "barefoot", "sunglasses"],
        "description": "包含比基尼、死庫水、連身泳裝與濕身海灘場景"
    },
    "主題:女僕與制服誘惑": {
        "core_tags": ["maid", "maid_headdress", "school_uniform", "serafuku", "office_lady", "nurse", "nun", "miko", "china_dress"],
        "related_tags": ["pencil_skirt", "pleated_skirt", "miniskirt", "necktie", "glasses", "gloves", "thighhighs"],
        "description": "包含女僕裝、水手服、OL套裝、護士、修女、巫女與旗袍等角色制服"
    },
    "主題:白髮銀髮美少女": {
        "core_tags": ["white_hair", "silver_hair", "grey_hair"],
        "related_tags": ["red_eyes", "blue_eyes", "purple_eyes", "long_hair", "twintails"],
        "description": "白髮、銀髮角色特徵聚合"
    },
    "主題:獸耳與奇幻娘": {
        "core_tags": ["animal_ears", "cat_ears", "cat_girl", "fox_ears", "fox_girl", "demon_girl", "elf", "pointy_ears", "horns", "wings", "tail"],
        "related_tags": ["halo", "fang", "collar", "choker"],
        "description": "貓娘、狐娘、魅魔、精靈耳與光環翅膀等奇幻屬性"
    },
    "主題:裸足與足部特寫": {
        "core_tags": ["barefoot", "soles", "toes", "feet", "footjob"],
        "related_tags": ["bare_legs", "sitting", "lying"],
        "description": "裸足、腳底、足趾特寫"
    },
    "主題:R-18與重度色氣": {
        "core_tags": ["explicit", "nude", "completely_nude", "sex", "vaginal", "oral", "fellatio", "paizuri", "cum", "cum_in_pussy", "spreading", "pussy", "ahegao", "nipples"],
        "related_tags": ["lingerie", "underwear", "no_panties", "no_bra", "pantyshot", "cameltoe", "topless", "bottomless", "bondage", "uncensored"],
        "description": "R-18 露點、性行為、內衣走光與無修正標籤聚合"
    }
}


def compute_tag_correlations(
    target_tag: str,
    min_cooccurrence: int = 2,
    top_k: int = 40,
) -> Dict[str, Any]:
    """
    計算指定標籤 (target_tag，支援輸入英文名或中文名) 在圖庫中的共現相關性：
    回傳最常一起出現的：
    - 畫師 (artists)
    - 角色 (characters)
    - 作品 (copyrights)
    - 相關屬性 (general)
    並計算 Jaccard Similarity 與條件機率 P(Tag_B | Target_Tag)。
    """
    conn = get_db()
    t_row = conn.execute(
        "SELECT id, name, category, zh_name, post_count FROM tags WHERE name = ? OR zh_name LIKE ? ORDER BY post_count DESC LIMIT 1",
        (target_tag.strip(), f"%{target_tag.strip()}%"),
    ).fetchone()

    if not t_row:
        conn.close()
        return {"found": False, "message": f"找不到標籤：{target_tag}"}

    target_id = t_row["id"]
    target_count = max(1, t_row["post_count"])

    rows = conn.execute(
        """
        SELECT t.id, t.name, t.category, t.zh_name, t.post_count, COUNT(*) as co_count
        FROM image_tags it1
        JOIN image_tags it2 ON it1.image_id = it2.image_id
        JOIN tags t ON t.id = it2.tag_id
        WHERE it1.tag_id = ? AND it2.tag_id != ?
        GROUP BY t.id
        HAVING co_count >= ?
        ORDER BY co_count DESC
        LIMIT ?
        """,
        (target_id, target_id, min_cooccurrence, top_k * 2),
    ).fetchall()
    conn.close()

    correlated = []
    for r in rows:
        co = r["co_count"]
        other_cnt = max(1, r["post_count"])
        # Conditional probability P(other | target)
        cond_prob = co / target_count
        # Jaccard similarity = |A ∩ B| / |A ∪ B|
        union_cnt = max(1, target_count + other_cnt - co)
        jaccard = co / union_cnt
        # Relevance score blending conditional probability and Jaccard
        relevance = round((cond_prob * 0.65 + jaccard * 0.35), 4)
        zh = r["zh_name"] or get_tag_zh(r["name"], r["category"])

        correlated.append({
            "name": r["name"],
            "zh_name": zh,
            "category": r["category"],
            "co_count": co,
            "total_count": other_cnt,
            "cond_prob_pct": round(cond_prob * 100, 1),
            "jaccard": round(jaccard, 3),
            "relevance_score": relevance,
        })

    correlated.sort(key=lambda x: (x["relevance_score"], x["co_count"]), reverse=True)
    correlated = correlated[:top_k]

    by_cat = defaultdict(list)
    for item in correlated:
        by_cat[item["category"]].append(item)

    return {
        "found": True,
        "target_tag": {
            "name": t_row["name"],
            "zh_name": t_row["zh_name"] or get_tag_zh(t_row["name"], t_row["category"]),
            "category": t_row["category"],
            "count": t_row["post_count"],
        },
        "correlated_all": correlated,
        "by_category": dict(by_cat),
    }


def semantic_search_expand(query_text: str) -> Dict[str, Any]:
    """
    智慧語意展開搜尋：
    當使用者輸入自然語言或關鍵字（如「黑絲 OL 巨乳」、「白髮兔女郎」），
    自動比對中英文字典、主題叢集與資料庫共現關係，展開成關聯標籤群組並回傳符合相關性的圖片！
    """
    q = query_text.strip().lower()
    if not q:
        return {"matched_tags": [], "related_tags": [], "images": []}

    conn = get_db()
    all_tags = conn.execute(
        "SELECT id, name, category, zh_name, post_count FROM tags WHERE post_count > 0"
    ).fetchall()

    # Split query into tokens
    tokens = [tok.strip() for tok in q.replace("、", " ").replace("，", " ").replace(",", " ").split() if tok.strip()]
    if not tokens:
        tokens = [q]

    direct_matched_tags = []
    matched_tag_names = set()

    # 1. Check semantic theme clusters
    expanded_keywords = set(tokens)
    for theme_name, info in SEMANTIC_THEME_CLUSTERS.items():
        for tok in tokens:
            if tok in theme_name.lower() or tok in info["description"].lower():
                for ct in info["core_tags"]:
                    expanded_keywords.add(ct.lower())

    # 2. Match against TAG_ZH_DICT & database tags
    for eng_k, zh_v in TAG_ZH_DICT.items():
        for tok in tokens:
            if tok in eng_k.lower() or tok in zh_v.lower():
                expanded_keywords.add(eng_k.lower())

    for r in all_tags:
        tname = r["name"]
        tzh = r["zh_name"] or get_tag_zh(tname, r["category"])
        tname_l = tname.lower()
        tzh_l = tzh.lower()

        hit = False
        for kw in expanded_keywords:
            if kw == tname_l or (len(kw) >= 2 and (kw in tname_l or kw in tzh_l)):
                hit = True
                break
        if hit and tname not in matched_tag_names:
            matched_tag_names.add(tname)
            direct_matched_tags.append({
                "id": r["id"],
                "name": tname,
                "zh_name": tzh,
                "category": r["category"],
                "count": r["post_count"],
            })

    direct_matched_tags.sort(key=lambda x: x["count"], reverse=True)

    # 3. Rank images by how many matched/expanded tags they contain + confidence score!
    ranked_images = []
    if direct_matched_tags:
        tag_ids = [t["id"] for t in direct_matched_tags[:50]]
        placeholders = ",".join(["?"] * len(tag_ids))
        img_rows = conn.execute(
            f"""
            SELECT i.id, i.rel_path, i.filename, i.folder, i.media_type, i.ai_tagged, i.suggested_folder,
                   COUNT(DISTINCT it.tag_id) as matched_tag_count,
                   SUM(it.confidence) as relevance_score
            FROM images i
            JOIN image_tags it ON it.image_id = i.id
            WHERE it.tag_id IN ({placeholders})
            GROUP BY i.id
            ORDER BY matched_tag_count DESC, relevance_score DESC, i.mtime DESC
            LIMIT 120
            """,
            tag_ids,
        ).fetchall()
        for ir in img_rows:
            ranked_images.append(dict(ir))

    # 4. Also find co-occurring related tags from top matched tag
    related_suggestions = []
    if direct_matched_tags:
        corr = compute_tag_correlations(direct_matched_tags[0]["name"], min_cooccurrence=2, top_k=15)
        for c_item in corr.get("correlated_all", []):
            if c_item["name"] not in matched_tag_names:
                related_suggestions.append(c_item)

    conn.close()
    return {
        "query": query_text,
        "matched_tags": direct_matched_tags[:30],
        "related_tags": related_suggestions[:15],
        "total_images_found": len(ranked_images),
        "images": ranked_images,
    }


def apply_semantic_theme_tags_batch() -> Dict[str, Any]:
    """
    自動掃描圖庫中所有已打標的圖片，根據標籤相關性叢集 (SEMANTIC_THEME_CLUSTERS)，
    自動為符合條件的圖片掛上高階中文主題標籤（如 `主題:黑絲與美腿`、`主題:兔女郎與緊身衣` 等），
    並補齊所有已知標籤的繁體中文翻譯。
    """
    conn = get_db()
    tag_cache: Dict[str, int] = {}
    for r in conn.execute("SELECT id, name FROM tags"):
        tag_cache[r["name"]] = r["id"]

    # Update any missing zh_name from TAG_ZH_DICT
    zh_updated = 0
    with conn:
        for r in conn.execute("SELECT id, name, category, zh_name FROM tags"):
            if not r["zh_name"]:
                zh = get_tag_zh(r["name"], r["category"])
                if zh:
                    conn.execute("UPDATE tags SET zh_name = ? WHERE id = ?", (zh, r["id"]))
                    zh_updated += 1

    theme_links = []
    themes_applied = {}

    for theme_name, info in SEMANTIC_THEME_CLUSTERS.items():
        core_tags = info["core_tags"]
        placeholders = ",".join(["?"] * len(core_tags))
        rows = conn.execute(
            f"""
            SELECT DISTINCT it.image_id
            FROM image_tags it
            JOIN tags t ON t.id = it.tag_id
            WHERE t.name IN ({placeholders})
            """,
            core_tags,
        ).fetchall()

        if rows:
            tid_theme = ensure_tag(conn, tag_cache, theme_name, "general")
            conn.execute("UPDATE tags SET zh_name = ? WHERE id = ?", (theme_name, tid_theme))
            for r in rows:
                theme_links.append((r["image_id"], tid_theme, 1.0, "semantic"))
            themes_applied[theme_name] = len(rows)

    with conn:
        if theme_links:
            conn.executemany(
                "INSERT OR IGNORE INTO image_tags (image_id, tag_id, confidence, source) VALUES (?, ?, ?, ?)",
                theme_links,
            )
        refresh_tag_counts(conn)

    conn.close()
    return {
        "zh_translations_updated": zh_updated,
        "theme_links_added": len(theme_links),
        "themes_summary": themes_applied,
    }


def call_gemini_flash_for_tags(prompt_task: str, api_key: Optional[str] = None) -> Dict[str, Any]:
    """
    若環境變數 GEMINI_API_KEY / GOOGLE_API_KEY 或傳入 api_key 存在，
    可直接呼叫 Gemini Flash 模型對圖庫中的標籤進行相關性語意分析與自動翻譯。
    同時也支援透過 Antigravity MCP 直接由 Antigravity Agent 的 Gemini Flash 處理。
    """
    key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    conn = get_db()
    top_tags = conn.execute(
        "SELECT name, category, zh_name, post_count FROM tags WHERE post_count > 0 ORDER BY post_count DESC LIMIT 200"
    ).fetchall()
    conn.close()

    tag_summary = [
        {"name": r["name"], "category": r["category"], "zh": r["zh_name"], "count": r["post_count"]}
        for r in top_tags
    ]

    if not key:
        return {
            "mode": "mcp_or_local_graph",
            "message": "已準備好標籤共現圖譜！你可以在 Antigravity 對話框直接請 Gemini Flash 透過 MCP 工具處理，或在下方填入 API Key 由網頁直接呼叫。",
            "top_tags_sample": tag_summary[:30],
        }

    import google.generativeai as genai
    genai.configure(api_key=key)
    model = genai.GenerativeModel("gemini-2.5-flash")

    sys_prompt = (
        "你是一個專門整理二次元插畫圖庫 (Twitter/Pixiv/Danbooru) 的標籤相關性 AI 助手。\n"
        f"目前圖庫的前 200 個高頻標籤如下：\n{json.dumps(tag_summary, ensure_ascii=False)}\n\n"
        f"使用者需求：{prompt_task}\n"
        "請回傳 JSON 格式：\n"
        "{\n"
        '  "analysis": "針對使用者需求的標籤相關性分析說明",\n'
        '  "recommended_include_tags": ["建議篩選的標籤name清單"],\n'
        '  "recommended_exclude_tags": ["建議排除的標籤name清單"],\n'
        '  "new_zh_translations": {"英文tag_name": "繁體中文翻譯"},\n'
        '  "suggested_theme_groups": [{"theme_name": "主題:名稱", "member_tags": ["tag1", "tag2"]}]\n'
        "}"
    )
    resp = model.generate_content(
        sys_prompt,
        generation_config={"response_mime_type": "application/json"},
    )
    parsed = json.loads(resp.text)

    # Automatically apply any new translations and theme groups returned by Gemini Flash!
    conn = get_db()
    tag_cache: Dict[str, int] = {}
    for r in conn.execute("SELECT id, name FROM tags"):
        tag_cache[r["name"]] = r["id"]

    with conn:
        for eng, zh in parsed.get("new_zh_translations", {}).items():
            conn.execute("UPDATE tags SET zh_name = ? WHERE name = ? AND zh_name = ''", (zh, eng))

        for tg in parsed.get("suggested_theme_groups", []):
            tname = tg.get("theme_name", "").strip()
            members = tg.get("member_tags", [])
            if tname and members:
                placeholders = ",".join(["?"] * len(members))
                rows = conn.execute(
                    f"SELECT DISTINCT it.image_id FROM image_tags it JOIN tags t ON t.id = it.tag_id WHERE t.name IN ({placeholders})",
                    members,
                ).fetchall()
                if rows:
                    tid = ensure_tag(conn, tag_cache, tname, "general")
                    conn.execute("UPDATE tags SET zh_name = ? WHERE id = ?", (tname, tid))
                    conn.executemany(
                        "INSERT OR IGNORE INTO image_tags (image_id, tag_id, confidence, source) VALUES (?, ?, 1.0, 'gemini_flash')",
                        [(r["image_id"], tid) for r in rows],
                    )
        refresh_tag_counts(conn)
    conn.close()

    return {
        "mode": "gemini_flash_direct",
        "result": parsed,
    }
