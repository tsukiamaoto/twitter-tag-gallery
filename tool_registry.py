"""One catalog and validated argument schema for Settings and both MCP transports."""
from dataclasses import dataclass
from functools import lru_cache
import importlib
import inspect
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


Tag = Annotated[str, Field(min_length=1, max_length=300)]
ImageId = Annotated[int, Field(gt=0)]
Confidence = Annotated[float, Field(ge=0, le=1)]
Similarity = Annotated[float, Field(ge=0, le=100)]
Scope = Literal["ai_low_conf", "small_folders_ai", "recent_root"]


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class Overview(Arguments):
    pass


class Correlations(Arguments):
    target_tag: Tag
    min_cooccurrence: int = Field(default=2, ge=1, le=1000000)
    top_k: int = Field(default=40, ge=1, le=200)


class Search(Arguments):
    query: str = Field(min_length=1, max_length=1000)


class Untranslated(Arguments):
    category: Literal["all", "artist", "general", "character", "copyright", "meta", "rating", "semantic"] = "all"
    limit: int = Field(default=100, ge=1, le=1000)


class Theme(Arguments):
    theme_name: Tag
    member_tags: list[Tag] = Field(min_length=1, max_length=300)


class Enrichments(Arguments):
    translations: dict[Tag, Tag] | None = Field(default=None, max_length=1000)
    semantic_theme_groups: list[Theme] | None = Field(default=None, max_length=100)
    auto_cluster_builtin_themes: bool = True


class TagOrMove(Arguments):
    include_tags: list[Tag] = Field(max_length=100)
    add_artist_tag: Tag | None = None
    add_general_tags: list[Tag] | None = Field(default=None, max_length=100)
    move_to_folder: str | None = Field(default=None, min_length=1, max_length=1000)
    only_root_files: bool = True


class Reverse(Arguments):
    image_id: ImageId
    min_similarity: Similarity = 72.0
    fallback_to_ai: bool = False


class Candidates(Arguments):
    scope: Scope = "ai_low_conf"
    limit: int = Field(default=30, ge=1, le=200)


class DeepReverse(Arguments):
    image_ids: list[ImageId] | None = Field(default=None, min_length=1, max_length=200)
    scope: Scope = "ai_low_conf"
    limit: int = Field(default=15, ge=1, le=200)
    min_similarity: Similarity = 72.0
    include_google_lens_url: bool = True
    auto_move_verified: bool = True


class Calibration(Arguments):
    image_id: ImageId
    verified_artist: Tag | None = None
    artist_zh_name: Tag | None = None
    twitter_handle: Tag | None = None
    characters: list[Tag] = Field(default_factory=list, max_length=100)
    copyrights: list[Tag] = Field(default_factory=list, max_length=100)
    add_general_tags: list[Tag] = Field(default_factory=list, max_length=100)
    zh_translations: dict[Tag, Tag] = Field(default_factory=dict, max_length=300)
    remove_tags: list[Tag] = Field(default_factory=list, max_length=100)
    purge_ai_artist_tags: bool = True
    move_to_artist_folder: bool = True


class Calibrations(Arguments):
    calibrations: list[Calibration] = Field(min_length=1, max_length=200)


class Purge(Arguments):
    max_ai_conf_to_purge: Confidence = 0.65
    revert_unverified_ai_folders: bool = True


class Organize(Arguments):
    execute_move: bool = False
    min_confidence: Confidence = 0.25
    min_images_per_artist: int = Field(default=1, ge=1, le=1000000)
    only_existing_folders: bool = False
    auto_rename_on_move: bool = True


class ImageSearch(Arguments):
    q: str = Field(default="", max_length=1000)
    include_tags: list[Tag] = Field(default_factory=list, max_length=20, description="需全部符合的原始標籤名稱")
    exclude_tags: list[Tag] = Field(default_factory=list, max_length=20)
    folder: str = Field(default="__ALL__", max_length=500, description="__ALL__ 全庫、__ROOT__ 根目錄，或資料夾名稱")
    media_type: Literal["all", "image", "gif", "video", "gif_video"] = "all"
    ai_status: Literal["all", "tagged", "untagged"] = "all"
    sort: Literal["natural_asc", "mtime_desc", "mtime_asc", "size_desc"] = "mtime_desc"
    page: int = Field(default=1, ge=1, le=100000)
    page_size: int = Field(default=20, ge=1, le=200)


class ImageDetails(Arguments):
    image_id: ImageId
    tag_limit: int = Field(default=100, ge=1, le=500)


class TagInput(Arguments):
    name: Tag
    category: Literal["artist", "general", "character", "copyright", "meta", "rating"] = "general"


class EditTags(Arguments):
    image_ids: list[ImageId] = Field(min_length=1, max_length=500)
    add_tags: list[TagInput] = Field(default_factory=list, max_length=100)
    remove_tag_names: list[Tag] = Field(default_factory=list, max_length=100)


class AnalyzeImages(Arguments):
    image_ids: list[ImageId] = Field(min_length=1, max_length=100)
    skip_completed: bool = True


class MoveImages(Arguments):
    image_ids: list[ImageId] = Field(min_length=1, max_length=500)
    target_folder: str = Field(min_length=1, max_length=500, description="圖庫內資料夾名稱；__ROOT__ 代表根目錄")
    auto_rename: bool = True
    execute_move: bool = Field(default=False, description="false 僅預覽；true 才搬移檔案")


class UndoRename(Arguments):
    batch_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


class JobList(Arguments):
    limit: int = Field(default=10, ge=1, le=50)


class ToolRun(Arguments):
    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class CancelJob(Arguments):
    job_id: str = Field(pattern=r"^[a-f0-9]{32}$")


@dataclass(frozen=True)
class Tool:
    name: str
    label: str
    description: str
    writes: bool
    arguments: type[Arguments]
    example: dict
    network: bool = False
    background: bool = False
    exclusive: bool = True
    audit: bool = True

    def schema(self):
        return self.arguments.model_json_schema()

    def validate(self, value):
        try:
            return self.arguments.model_validate(value).model_dump()
        except ValidationError as error:
            # Never echo submitted values (future tools may accept credentials).
            messages = [f"{'.'.join(map(str, item['loc'])) or 'arguments'}: {item['msg']}"
                        for item in error.errors(include_input=False, include_context=False)[:8]]
            raise ValueError("工具參數無效：" + "；".join(messages)) from None

    def handler(self):
        workflows = importlib.import_module("gallery_workflow_tools")
        function = getattr(workflows, "tool_" + self.name, None)
        if function is not None:
            return function
        module = importlib.import_module("gallery_tools")
        if self.name == "gallery_reverse_image_search":
            return lambda **args: module.reverse_search_and_tag_image(**args, include_google_lens_url=True)
        return getattr(module, "tool_" + self.name)

    def protocol(self):
        return {"name": self.name, "title": self.label, "description": self.description,
                "inputSchema": self.schema(), "annotations": {"readOnlyHint": not self.writes,
                "destructiveHint": self.writes, "openWorldHint": self.network}}


_tools = [
    Tool("gallery_get_overview", "圖庫總覽", "取得圖片數量、歸檔情況、熱門畫師與標籤。", False, Overview, {}),
    Tool("gallery_find_tag_correlations", "標籤相關性", "計算指定標籤或畫師的共現標籤與相關性。", False, Correlations, {"target_tag": "landscape", "top_k": 40}),
    Tool("gallery_semantic_search", "語意搜尋", "依關鍵字與中英對照展開搜尋，回傳相關圖片與標籤。", False, Search, {"query": "風景"}),
    Tool("gallery_list_untranslated_tags", "待翻譯標籤", "列出尚未有繁體中文翻譯的高頻標籤。", False, Untranslated, {"category": "all", "limit": 100}),
    Tool("gallery_list_candidates_for_artist_calibration", "待校正圖片", "列出待驗證或 AI 低信心度畫師圖片，包含檔案路徑。", False, Candidates, {"scope": "ai_low_conf", "limit": 30}),
    Tool("gallery_apply_gemini_enrichments", "寫入翻譯與主題", "儲存標籤翻譯、主題群組，並可重新套用內建主題。", True, Enrichments, {"translations": {"landscape": "風景"}, "auto_cluster_builtin_themes": False}),
    Tool("gallery_batch_tag_or_move", "批次標籤與搬移", "篩選符合全部指定標籤的圖片，新增標籤或搬移到目標資料夾。", True, TagOrMove, {"include_tags": ["landscape"], "add_general_tags": ["風景"], "only_root_files": True}),
    Tool("gallery_reverse_image_search", "單張反查與校正", "查詢外部圖片來源並寫入認證畫師與標籤；可使用本機 AI 作備援。", True, Reverse, {"image_id": 1, "fallback_to_ai": False}, True),
    Tool("gallery_deep_reverse_search_batch", "批次反查與歸檔", "批次查詢外部來源、校正標籤，並可搬移已認證圖片。", True, DeepReverse, {"image_ids": [1], "auto_move_verified": False}, True),
    Tool("gallery_calibrate_image_artist_and_tags", "寫入畫師校正", "寫入已驗證的畫師與標籤；依參數清除 AI 畫師標籤並搬移圖片。", True, Calibrations, {"calibrations": [{"image_id": 1, "verified_artist": "artist_name", "move_to_artist_folder": False}]}),
    Tool("gallery_purge_all_low_conf_ai_artists", "清理畫師與資料夾", "清除低信心度 AI 畫師標籤，並可還原因此誤建的資料夾。", True, Purge, {"max_ai_conf_to_purge": 0.65, "revert_unverified_ai_folders": False}),
    Tool("gallery_auto_organize_artist_folders", "畫師歸檔", "依認證畫師預覽或執行歸檔；execute_move=false 僅預覽。", True, Organize, {"execute_move": False, "min_confidence": 0.25}),
    Tool("gallery_scan_library", "掃描圖庫", "掃描目前設定的圖庫並同步索引；保留人工標籤，完整掃描才標記遺失。回傳 run_id，使用 gallery_get_tool_run 查詢結果。", True, Overview, {}, background=True),
    Tool("gallery_search_images", "搜尋圖片", "以關鍵字、標籤、資料夾及 AI 狀態分頁查詢圖片，取得後續操作所需的 image_id。", False, ImageSearch, {"q": "風景", "page_size": 20}),
    Tool("gallery_get_image_details", "圖片詳細資料", "取得圖片標籤、處理狀態、遺失狀態及圖庫內路徑；tag_limit 限制回傳標籤數。", False, ImageDetails, {"image_id": 1}),
    Tool("gallery_update_image_tags", "編輯圖片標籤", "對明確指定的圖片新增人工標籤或移除指定標籤名稱，不會搬移檔案。整批 ID 必須存在。", True, EditTags, {"image_ids": [1], "add_tags": [{"name": "landscape", "category": "general"}]}),
    Tool("gallery_analyze_images", "本機 AI 打標", "對指定圖片執行本機視覺打標，保留人工標籤。首次使用可能下載模型；預設略過相同檔案與模型版本已完成項目。回傳 run_id。", True, AnalyzeImages, {"image_ids": [1], "skip_completed": True}, network=True, background=True),
    Tool("gallery_move_images", "搬移指定圖片", "預覽或搬移明確指定的圖片至圖庫內資料夾，使用可復原檔案日誌。execute_move=false 預覽，true 才搬移。回傳 run_id。", True, MoveImages, {"image_ids": [1], "target_folder": "artist_name", "execute_move": False}, background=True),
    Tool("gallery_recover_file_operations", "復原中斷檔案操作", "檢查並修復尚未完成的檔案操作日誌；不會撤銷已完成的正常搬移。回傳 run_id。", True, Overview, {}, background=True),
    Tool("gallery_list_rename_batches", "可還原改名批次", "列出最近可還原的已完成改名批次，供 gallery_undo_rename 使用。", False, Overview, {}),
    Tool("gallery_undo_rename", "還原已完成改名", "依 gallery_list_rename_batches 回傳的 batch_id 還原改名；遇到檔案衝突會停止而不覆寫。回傳 run_id。", True, UndoRename, {"batch_id": "replace_with_existing_batch_id"}, background=True),
    Tool("gallery_get_jobs", "查詢圖庫任務", "取得跨程序目前作業、進度及最近任務。輪詢不建立額外工具執行紀錄。", False, JobList, {"limit": 10}, audit=False),
    Tool("gallery_get_tool_run", "查詢工具執行結果", "依 run_id 取得背景工具狀態、job_id、結果與錯誤；執行中可每 1–2 秒查詢。輪詢不建立額外紀錄。", False, ToolRun, {"run_id": "0" * 32}, audit=False),
    Tool("gallery_cancel_job", "要求停止任務", "使用目前作業的 job_id 要求合作式停止，等待安全步驟結束；不強制中止檔案操作。", True, CancelJob, {"job_id": "0" * 32}, exclusive=False),
]
CATALOG = {tool.name: tool for tool in _tools}
TOOLS = {tool.name: (tool.label, tool.writes) for tool in _tools}


def get_tool(name):
    if name not in CATALOG:
        raise KeyError("未知的 MCP 工具")
    return CATALOG[name]


def catalog():
    from integration_settings import allowed, load
    config = load()
    return [{**tool.protocol(), "label": tool.label, "writes": tool.writes, "network": tool.network,
             "enabled": allowed(tool.name, config), "example": tool.example,
             "background": tool.background, "exclusive": tool.exclusive} for tool in _tools]


@lru_cache(maxsize=1)
def sdk_functions():
    """Expose the same model constraints to SDK introspection and older clients."""
    result = []
    for tool in _tools:
        def run(_tool=tool, **arguments):
            from tool_runtime import dispatch
            return dispatch(_tool.name, arguments)
        run.__name__ = tool.name
        run.__doc__ = tool.description
        parameters = [inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY,
                       default=inspect.Parameter.empty if field.is_required() else field.get_default(call_default_factory=True),
                       annotation=field.rebuild_annotation()) for name, field in tool.arguments.model_fields.items()]
        run.__signature__ = inspect.Signature(parameters, return_annotation=dict)
        run.__annotations__ = {param.name: param.annotation for param in parameters}
        result.append((tool, run))
    return result
