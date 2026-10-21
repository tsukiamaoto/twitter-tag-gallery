"""Category limits for generated tags; human edits remain unrestricted."""

AUTOMATIC_CATEGORIES = ("artist", "character", "copyright", "source")
POLICY_MESSAGE = "自動標籤僅允許畫師、角色、作品、來源；一般描述、分級、年份與主題請勿自動寫入"


def allows_tag(category, source):
    return source == "manual" or category in AUTOMATIC_CATEGORIES


def require_model_tags(tags):
    if any(not allows_tag(tag.get("category", "general"), "gemini_mcp") for tag in tags):
        raise ValueError(POLICY_MESSAGE)


def reject_generated_details(*values):
    if any(values):
        raise ValueError(POLICY_MESSAGE)


def summary():
    return {"automatic_categories": list(AUTOMATIC_CATEGORIES),
            "manual_categories_unrestricted": True, "message": POLICY_MESSAGE}
