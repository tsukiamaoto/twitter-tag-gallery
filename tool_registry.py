"""One catalog and validated argument schema for Settings and both MCP transports."""
from dataclasses import dataclass
from functools import lru_cache
import importlib
import inspect
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, ValidationError, model_validator


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
    target_tag: Tag = Field(description="要分析的既有原始標籤名稱或繁中名稱")
    min_cooccurrence: int = Field(default=2, ge=1, le=1000000, description="至少共同出現於幾張圖片")
    top_k: int = Field(default=40, ge=1, le=200, description="最多回傳幾個相關標籤")


class Search(Arguments):
    query: str = Field(min_length=1, max_length=1000, description="搜尋關鍵字；依既有中英標籤對照展開，不呼叫語言模型")


class Untranslated(Arguments):
    category: Literal["all", "artist", "general", "character", "copyright", "meta", "rating", "semantic"] = Field(default="all", description="要列出的既有標籤分類；all 為全部，讀取不代表允許生成該分類")
    limit: int = Field(default=100, ge=1, le=1000, description="最多回傳幾個待翻譯標籤")


class Theme(Arguments):
    theme_name: Tag = Field(description="舊版主題名稱；主題寫入已停用")
    member_tags: list[Tag] = Field(min_length=1, max_length=300, description="舊版主題成員；主題寫入已停用")


class Enrichments(Arguments):
    translations: dict[Tag, Tag] | None = Field(default=None, max_length=1000, description="既有原始標籤名稱到繁中名稱的對照；不建立新標籤")
    semantic_theme_groups: list[Theme] | None = Field(default=None, max_length=0, description="已停用自動主題標籤，請省略")
    auto_cluster_builtin_themes: Literal[False] = Field(default=False, description="已停用主題聚合；只能省略或傳 false")


class TagOrMove(Arguments):
    include_tags: list[Tag] = Field(min_length=1, max_length=100, description="必須全部符合的既有標籤名稱或繁中名稱；禁止空陣列")
    add_artist_tag: Tag | None = Field(default=None, description="新增至所有符合圖片的已驗證畫師名稱；省略則不新增")
    add_general_tags: list[Tag] | None = Field(default=None, max_length=0, description="僅保留核心標籤，請省略")
    move_to_folder: str | None = Field(default=None, min_length=1, max_length=1000, description="指定即實際搬移並自動改名，省略不搬移；需預覽請用 gallery_move_images")
    only_root_files: bool = Field(default=True, description="true 僅匹配根目錄；false 在全庫匹配 include_tags")


class Reverse(Arguments):
    image_id: ImageId = Field(description="由 gallery_search_images 取得的圖片 ID")
    min_similarity: Similarity = Field(default=72.0, description="外部反查命中的最低相似度，單位為百分比（0–100）")
    fallback_to_ai: bool = Field(default=False, description="外部反查未命中時是否執行本機 AI 打標")


class Candidates(Arguments):
    scope: Scope = Field(default="ai_low_conf", description="ai_low_conf=未認證的 AI 畫師；small_folders_ai=小資料夾內未認證圖片；recent_root=根目錄最近未認證圖片")
    limit: int = Field(default=30, ge=1, le=200, description="最多回傳幾張待校正圖片")


class DeepReverse(Arguments):
    image_ids: list[ImageId] | None = Field(default=None, min_length=1, max_length=200, description="明確指定要反查的圖片；省略才依 scope 與 limit 挑選")
    scope: Scope = Field(default="ai_low_conf", description="省略 image_ids 時使用的待校正範圍，含義同 gallery_list_candidates_for_artist_calibration")
    limit: int = Field(default=15, ge=1, le=200, description="省略 image_ids 時最多處理幾張；不限制明確指定的 ID")
    min_similarity: Similarity = Field(default=72.0, description="外部反查最低相似度，百分比（0–100）")
    include_google_lens_url: bool = Field(default=True, description="是否在逐圖結果附上 Google Lens 搜尋網址")
    auto_move_verified: bool = Field(default=False, description="明確傳 true 才搬移已認證圖片並自動改名；預設只校正標籤")


class Calibration(Arguments):
    image_id: ImageId = Field(description="要校正的圖片 ID")
    verified_artist: Tag | None = Field(default=None, description="已查證的畫師名稱；不能根據畫風猜測")
    artist_zh_name: Tag | None = Field(default=None, description="verified_artist 的繁中或日文常用名稱；省略不改翻譯")
    twitter_handle: Tag | None = Field(default=None, description="已確認的作者 X/Twitter 帳號，可含 @")
    characters: list[Tag] = Field(default_factory=list, max_length=100, description="新增已確認的角色；也會清除信心度低於 0.70 的 AI 角色標籤")
    copyrights: list[Tag] = Field(default_factory=list, max_length=100, description="新增已確認的作品；也會清除信心度低於 0.70 的 AI 作品標籤")
    add_general_tags: list[Tag] = Field(default_factory=list, max_length=0, description="僅保留核心標籤，請省略")
    zh_translations: dict[Tag, Tag] = Field(default_factory=dict, max_length=300, description="既有標籤名稱到繁中翻譯的對照")
    remove_tags: list[Tag] = Field(default_factory=list, max_length=100, description="明確移除的錯誤標籤名稱或繁中名稱；人工標籤保留")
    purge_ai_artist_tags: bool = Field(default=False, description="明確傳 true 才清除本張圖片所有 source=ai 的畫師標籤；新增畫師不會隱含啟用")
    move_to_artist_folder: bool = Field(default=False, description="明確傳 true 才搬移並改名至已確認身分的畫師資料夾；只校正標籤請省略")


class Calibrations(Arguments):
    calibrations: list[Calibration] = Field(min_length=1, max_length=200, description="逐圖校正內容；每項獨立指定，人工標籤保留")


class Purge(Arguments):
    max_ai_conf_to_purge: Confidence = Field(default=0.65, description="清除低於此門檻的 AI 畫師；已有認證畫師時也清除冗餘 AI 畫師")
    revert_unverified_ai_folders: bool = Field(default=False, description="明確傳 true 才將未認證的 AI 資料夾圖片搬回根目錄並刪除空資料夾")


class Organize(Arguments):
    execute_move: bool = Field(default=False, description="false 僅預覽；true 才依畫師歸檔")
    min_confidence: Confidence = Field(default=0.25, description="畫師標籤最低信心度（0–1）；仍須通過身分驗證")
    min_images_per_artist: int = Field(default=1, ge=1, le=1000000, description="每位畫師至少匹配幾張圖片才列入歸檔")
    only_existing_folders: bool = Field(default=False, description="true 僅歸入已存在的畫師資料夾")
    auto_rename_on_move: bool = Field(default=True, description="執行歸檔時是否按現有命名規則自動改名")


class ImageSearch(Arguments):
    q: str = Field(default="", max_length=1000, description="檔名、資料夾名稱或標籤搜尋關鍵字；空字串不限制關鍵字")
    include_tags: list[Tag] = Field(default_factory=list, max_length=20, description="需全部符合的原始標籤名稱")
    exclude_tags: list[Tag] = Field(default_factory=list, max_length=20, description="任一符合即排除的原始標籤名稱")
    folder: str = Field(default="__ALL__", max_length=500, description="__ALL__ 全庫、__ROOT__ 根目錄，或資料夾名稱")
    media_type: Literal["all", "image", "gif", "video", "gif_video"] = Field(default="all", description="媒體類型；gif_video 為 GIF 與影片，all 不限制")
    ai_status: Literal["all", "tagged", "untagged"] = Field(default="all", description="依本機 AI 已處理或未處理狀態篩選；all 不限制")
    sort: Literal["natural_asc", "mtime_desc", "mtime_asc", "size_desc"] = Field(default="mtime_desc", description="自然檔名升序、修改時間降序/升序，或檔案大小降序")
    page: int = Field(default=1, ge=1, le=100000, description="頁碼，從 1 開始")
    page_size: int = Field(default=20, ge=1, le=200, description="每頁最多回傳幾張圖片")


class ImageDetails(Arguments):
    image_id: ImageId = Field(description="由搜尋取得的圖片 ID；允許讀取已標記遺失的索引")
    tag_limit: int = Field(default=100, ge=1, le=500, description="最多回傳幾個標籤；不足全部時 tags_truncated=true")


class ArtistIdentityQuery(Arguments):
    artist_name: str = Field(default="", max_length=300, description="待查畫師名稱或帳號；可搭配作者個人頁網址")
    profile_urls: list[Tag] = Field(default_factory=list, max_length=10, description="待查 X/Twitter、Pixiv 或 Fanbox 作者個人頁網址")
    force_refresh: bool = Field(default=False, description="是否略過查證失敗的重試等待時間；仍先讀取已確認身分")


class ArtistIdentityStatus(Arguments):
    limit: int = Field(default=20, ge=1, le=100, description="最多回傳幾個畫師身分待查請求")


class ArtistIdentityEvidence(Arguments):
    source_url: Tag = Field(description="已閱讀且含外連依據的作者個人頁網址，必須列於 profile_urls")
    linked_url: Tag = Field(description="source_url 上由作者明確連到的另一個帳號個人頁，必須列於 profile_urls")


class ArtistIdentityConfirmation(Arguments):
    request_key: str = Field(pattern=r"^[a-f0-9]{64}$", description="由畫師身分查詢或待查佇列取得的 64 碼 request_key；不是 job_id 或 run_id")
    canonical_name: str = Field(min_length=1, max_length=200, description="查證後採用的畫師標準名稱")
    aliases: list[Tag] = Field(default_factory=list, max_length=80, description="已證實屬於同一人的其他名稱，須涵蓋原待查名稱")
    profile_urls: list[Tag] = Field(min_length=2, max_length=30, description="至少兩個不同平台的作者帳號，須涵蓋原待查帳號")
    evidence: list[ArtistIdentityEvidence] = Field(min_length=1, max_length=60, description="作者頁互連依據；所有提交帳號必須由這些連結連通")


class TagInput(Arguments):
    name: Tag = Field(description="已確認的原始標籤名稱")
    category: Literal["artist", "character", "copyright", "source"] = Field(description="核心分類：畫師、角色、作品或來源；不得以核心分類偽裝一般描述")


class EditTags(Arguments):
    image_ids: list[ImageId] = Field(min_length=1, max_length=500, description="要編輯的圖片 ID；任一 ID 不存在則整批拒絕")
    add_tags: list[TagInput] = Field(default_factory=list, max_length=100, description="新增的核心標籤，記為模型來源並保留既有人工標籤")
    remove_tag_names: list[Tag] = Field(default_factory=list, max_length=100, description="移除指定原始標籤名稱；人工標籤不會移除")


class AnalyzeImages(Arguments):
    image_ids: list[ImageId] = Field(min_length=1, max_length=100, description="要執行本機 AI 打標的圖片、GIF 或 MP4 ID")
    skip_completed: bool = Field(default=True, description="true 略過相同檔案及模型版本已完成的項目；false 重新分析")


class MoveImages(Arguments):
    image_ids: list[ImageId] = Field(min_length=1, max_length=500, description="要預覽或搬移的圖片 ID；所有 ID 必須存在且未標記遺失")
    target_folder: str = Field(min_length=1, max_length=500, description="圖庫內資料夾名稱；__ROOT__ 代表根目錄")
    auto_rename: bool = Field(default=True, description="搬移時是否依既有規則改名；預覽使用相同規則")
    execute_move: bool = Field(default=False, description="false 僅預覽；true 才搬移檔案")


class UndoRename(Arguments):
    batch_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$", description="gallery_list_rename_batches 回傳的既有 batch_id，不能使用 run_id")


class CleanDuplicates(Arguments):
    mode: Literal["both", "exact", "visual"] = Field(
        default="both",
        description="both=完全重複與靜態圖視覺相似；exact=SHA-256 加位元組驗證，可處理動畫；visual=靜態圖感知雜湊加 SSIM/MAE，排除動畫，候選需人工檢視細微差分",
    )
    scope: Literal["same_folder", "all"] = Field(
        default="same_folder",
        description="same_folder=僅比對同一資料夾內的重複/相似圖；all=跨資料夾全圖庫比對",
    )
    folder: str = Field(
        default="__ALL__",
        max_length=500,
        description="__ALL__=全圖庫；__ROOT__=僅根目錄；或指定資料夾名稱",
    )
    execute_delete: bool = Field(
        default=False,
        description="false 僅預覽重複群組與可釋放空間；true 才自動合併標籤、建立資料庫備份並將較低畫質副本移至系統資源回收筒",
    )
    max_mae: StrictFloat = Field(default=6.5, ge=0.5, le=20.0, description="視覺相似最大平均像素誤差 (MAE)，預設 6.5")
    min_ssim: StrictFloat = Field(default=0.95, ge=0.80, le=1.0, description="視覺相似最低結構相似度 (SSIM)，預設 0.95")
    max_diff_frac: StrictFloat = Field(
        default=0.005,
        ge=0.0001,
        le=0.05,
        description="縮至 512x512 後任一色彩通道差異超過 45/255 的像素比例上限（0.005=0.5%）；更小或低對比差分仍可能通過，不能保證辨識所有差分圖",
    )
    max_groups: int = Field(default=200, ge=1, le=1000, description="回傳群組上限；不限制執行刪除數量，截斷時 groups_truncated=true")



class JobList(Arguments):
    limit: int = Field(default=10, ge=1, le=50, description="最多回傳幾個共用圖庫作業；不包含桌面查證工作")


class ToolRun(Arguments):
    run_id: str = Field(pattern=r"^[a-f0-9]{32}$", description="工具啟動回應的 run_id；僅用於讀取工具執行狀態與 run.result，不是停止用的 job_id")


class CancelJob(Arguments):
    job_id: str = Field(pattern=r"^[a-f0-9]{32}$", description="工具啟動回應非 null 的 job_id，或 gallery_get_jobs 的 active.id；不是 run_id 或桌面查證工作 ID")


class AgentJob(Arguments):
    job_id: str = Field(pattern=r"^[a-f0-9]{32}$", description="桌面橋接器提供的查證工作 ID（agent_job_id），必須已領取並執行；不是工具 run_id 或共用作業 job_id")


class CancelAgentJob(Arguments):
    agent_job_id: str = Field(pattern=r"^[a-f0-9]{32}$", description="要停止的桌面查證工作 ID；由橋接器提供，或由背景工具啟動回應的 agent_job_id 取得")


class PublicationBatch(AgentJob):
    image_ids: list[ImageId] = Field(min_length=1, max_length=50, description="本桌面查證工作範圍內的圖片 ID，每批最多 50 張")


class PublicationSource(AgentJob):
    image_id: ImageId = Field(description="本桌面查證工作範圍內的圖片 ID")
    source_url: str = Field(min_length=10, max_length=2000, description="已逐圖核對的作者原貼文或 Pixiv 原作品網址")
    image_url: str = Field(min_length=10, max_length=2000, description="該原作品中與本機圖片匹配的圖片 URL，須核對頁碼")
    author_profile_url: str = Field(min_length=10, max_length=2000, description="原作品作者的個人頁網址")
    match_reason: str = Field(min_length=20, max_length=4000, description="說明圖片、作品頁碼與作者身分的核對依據；不得提交猜測日期")


class ArtistEvidence(AgentJob):
    model_config = ConfigDict(json_schema_extra={"allOf": [{
        "if": {"properties": {"confirmed": {"const": True}}, "required": ["confirmed"]},
        "then": {"required": ["canonical_name", "profile_urls", "evidence"], "properties": {
            "canonical_name": {"minLength": 1}, "profile_urls": {"minItems": 2}, "evidence": {"minItems": 1}}},
    }]})
    confirmed: bool = Field(description="true 提交有依據的身分，必須提供非空標準名稱、至少兩個不同平台帳號及互連證據；false 回報無法確認")
    canonical_name: str = Field(default="", max_length=200, description="confirmed=true 時必填的畫師標準名稱")
    aliases: list[Tag] = Field(default_factory=list, max_length=80, description="已確認的別名，須涵蓋工作原待查名稱")
    profile_urls: list[Tag] = Field(default_factory=list, max_length=30, description="confirmed=true 時須提供至少兩個不同平台帳號，涵蓋工作原待查帳號")
    evidence: list[ArtistIdentityEvidence] = Field(default_factory=list, max_length=60, description="confirmed=true 時至少一筆作者頁互連證據，所有帳號必須連通")

    @model_validator(mode="after")
    def require_confirmed_evidence(self):
        if self.confirmed and (not self.canonical_name or len(self.profile_urls) < 2 or not self.evidence):
            raise ValueError("confirmed=true 需要 canonical_name、至少兩個 profile_urls 及 evidence")
        return self


class McpServerId(Arguments):
    server_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$", description="MCP 管理卡片 ID，由 gallery_list_mcp_servers 取得")


class DeleteMcpServer(McpServerId):
    sync_client: bool = Field(default=False, description="true 同時更新 Antigravity 客戶端設定；false 僅更新圖庫設定")


class SaveMcpServer(Arguments):
    server_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$", description="既有卡片 ID 或新自訂卡片 ID；省略其他欄位保留既有值")
    name: str | None = Field(default=None, min_length=1, max_length=100, description="卡片顯示名稱")
    category: str | None = Field(default=None, max_length=100, description="卡片分類名稱")
    description: str | None = Field(default=None, max_length=1000, description="卡片用途說明")
    transport: Literal["stdio", "http", "sse"] | None = Field(default=None, description="連線方式；stdio 使用指令，http/sse 使用網址")
    enabled: bool | None = Field(default=None, description="是否啟用卡片；內建卡片會同步對應圖庫或背景查證設定")
    read_only: bool | None = Field(default=None, description="是否套用唯讀限制；自訂服務的實際限制仍由該服務執行")
    command: str | None = Field(default=None, max_length=1000, description="stdio 啟動程式；診斷時會實際執行此程式")
    args: list[str] | None = Field(default=None, max_length=100, description="stdio 程式參數，每項為獨立參數")
    cwd: str | None = Field(default=None, max_length=1000, description="stdio 程式的工作目錄")
    env: dict[str, str] | None = Field(default=None, max_length=100, description="stdio 子程序環境變數；省略保留既有設定")
    url: str | None = Field(default=None, max_length=2000, description="HTTP 或 SSE 服務端點網址")
    headers: dict[str, str] | None = Field(default=None, max_length=100, description="HTTP 或 SSE 額外請求標頭")
    disabled_tools: list[Tag] | None = Field(default=None, max_length=200, description="要停用的工具名稱；空陣列清除停用清單")
    custom_config: dict[str, str | int | float | bool | list[str]] | None = Field(default=None, description="客戶端擴充連線設定，依既有設定驗證規則處理")
    sync_client: bool = Field(default=False, description="true 同步至 Antigravity 設定檔；false 僅儲存圖庫卡片")


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
        from prompt_store import instruction
        description = instruction(self)
        if self.background:
            description += ("\n執行契約：立即回傳 status=running 與 run_id；這不是業務完成結果。"
                            "用 gallery_get_tool_run(run_id) 查詢，至 run.status 不再是 running，讀取 run.result 或 run.error。"
                            "停止時使用啟動回應的 cancel_tool 與 cancel_arguments。"
                            "job_id 是共用圖庫作業 ID，可能為 null；agent_job_id 是桌面查證工作 ID，兩者不可混用。"
                            "保持 MCP 連線直到工具結束。")
        return {"name": self.name, "title": self.label, "description": description,
                "inputSchema": self.schema(), "annotations": {"readOnlyHint": not self.writes,
                "destructiveHint": self.writes, "openWorldHint": self.network}}


_tools = [
    Tool("gallery_get_agent_job", "讀取 Antigravity 查證工作", "讀取專用工作指示、最多 50 張待查圖片、原始檔名與線索。工作必須已由桌面橋接器領取。", False, AgentJob, {"job_id": "0" * 32}),
    Tool("gallery_trace_publication_dates", "查詢原始發布日期", "使用 Danbooru MD5、Pixiv ID 找原作品並查證發布日期；不使用收錄日或檔案日期，不修改標籤或搬移圖片。回傳 run_id。", True, PublicationBatch, {"job_id": "0" * 32, "image_ids": [1]}, network=True, background=True, exclusive=False),
    Tool("gallery_submit_publication_sources", "提交原作品來源", "提交已逐圖比對的原貼文、作品圖片及作者網址與核對依據；後端重新取得發布日期，不接受模型填寫日期。", True, PublicationSource, {"job_id": "0" * 32, "image_id": 1, "source_url": "https://www.pixiv.net/artworks/123", "image_url": "https://i.pximg.net/original.jpg", "author_profile_url": "https://www.pixiv.net/users/123", "match_reason": "已核對原作品第零頁圖片與作者個人頁，與本機圖片內容相同。"}, network=True, background=True, exclusive=False),
    Tool("gallery_complete_agent_batch", "完成查證並安全改名", "完成指定批次，依已確認發布日期對受影響資料夾重新編號與安全改名；未知圖片保留原名，保留還原紀錄。", True, PublicationBatch, {"job_id": "0" * 32, "image_ids": [1]}, background=True),
    Tool("gallery_submit_artist_identity", "提交畫師身分依據", "提交作者個人頁互相連結的畫師身分依據；必須涵蓋原待查名稱與帳號，保留人工標籤及既有圖片位置。", True, ArtistEvidence, {"job_id": "0" * 32, "confirmed": False}),
    Tool("gallery_resolve_artist_identity", "查證畫師跨平台身分", "先查已確認的別名，再查 Danbooru；查不到回傳 pending_model 與 request_key。請由目前模型搜尋作者頁面後，以 gallery_confirm_artist_identity 保存依據。圖庫不啟動模型 CLI，不搬移既有圖片。", True, ArtistIdentityQuery, {"artist_name": "artist_name", "profile_urls": ["https://www.pixiv.net/users/123"]}, network=True, background=True),
    Tool("gallery_get_artist_identity_status", "畫師身分待補查佇列", "讀取已確認身分數與待查 request_key、名稱及平台網址。請由目前 MCP 模型搜尋並閱讀作者頁面，確認後呼叫 gallery_confirm_artist_identity；無依據就保留待查。", False, ArtistIdentityStatus, {"limit": 20}),
    Tool("gallery_confirm_artist_identity", "保存畫師跨平台身分", "提交待查 request_key 的查證結果。至少兩個不同平台帳號，提供作者頁面 source_url 連到 linked_url 的依據，涵蓋全部帳號及原查詢。永久記住別名與共用資料夾；不搬移既有圖片，不啟動 CLI。", True, ArtistIdentityConfirmation, {"request_key": "0" * 64, "canonical_name": "artist_name", "aliases": ["other_name"], "profile_urls": ["https://www.pixiv.net/users/123", "https://x.com/artist_name"], "evidence": [{"source_url": "https://www.pixiv.net/users/123", "linked_url": "https://x.com/artist_name"}]}),
    Tool("gallery_get_overview", "圖庫總覽", "取得圖片數量、歸檔情況、熱門畫師與標籤。", False, Overview, {}),
    Tool("gallery_find_tag_correlations", "標籤相關性", "計算指定標籤或畫師的共現標籤與相關性。", False, Correlations, {"target_tag": "landscape", "top_k": 40}),
    Tool("gallery_semantic_search", "語意搜尋", "依關鍵字與中英對照展開搜尋，回傳相關圖片與標籤。", False, Search, {"query": "風景"}),
    Tool("gallery_list_untranslated_tags", "待翻譯標籤", "列出尚未有繁體中文翻譯的高頻標籤。", False, Untranslated, {"category": "all", "limit": 100}),
    Tool("gallery_list_candidates_for_artist_calibration", "待校正圖片", "列出待驗證或 AI 低信心度畫師圖片，包含檔案路徑。", False, Candidates, {"scope": "ai_low_conf", "limit": 30}),
    Tool("gallery_apply_gemini_enrichments", "寫入標籤翻譯", "更新既有標籤翻譯；自動主題群組已停用，不得產生一般描述標籤。", True, Enrichments, {"translations": {"artist_name": "畫師名稱"}}),
    Tool("gallery_batch_tag_or_move", "批次標籤與搬移", "篩選同時符合 include_tags 全部標籤的圖片（至少提供一個標籤），新增畫師或實際搬移到目標資料夾；空篩選拒絕。指定 move_to_folder 即搬移並自動改名，需預覽請用 gallery_move_images；禁止新增一般描述標籤。", True, TagOrMove, {"include_tags": ["artist_name"], "add_artist_tag": "artist_alias", "only_root_files": True}),
    Tool("gallery_reverse_image_search", "單張反查與校正", "查詢外部圖片來源並寫入認證畫師與標籤；可使用本機 AI 作備援。", True, Reverse, {"image_id": 1, "fallback_to_ai": False}, True),
    Tool("gallery_deep_reverse_search_batch", "批次反查與歸檔", "批次查詢外部來源、校正標籤，並可搬移已認證圖片。", True, DeepReverse, {"image_ids": [1], "auto_move_verified": False}, True),
    Tool("gallery_calibrate_image_artist_and_tags", "寫入畫師校正", "僅寫入已驗證的畫師、角色與作品；保留人工標籤。預設不搬移、不清除 AI 畫師；僅在明確啟用參數時執行。提交角色/作品會清除對應分類低於 0.70 的 AI 標籤。禁止一般描述或主題標籤。", True, Calibrations, {"calibrations": [{"image_id": 1, "verified_artist": "artist_name", "move_to_artist_folder": False}]}, network=True),
    Tool("gallery_purge_all_low_conf_ai_artists", "清理畫師與資料夾", "清除低信心度 AI 畫師標籤，並可還原因此誤建的資料夾。", True, Purge, {"max_ai_conf_to_purge": 0.65, "revert_unverified_ai_folders": False}),
    Tool("gallery_auto_organize_artist_folders", "畫師歸檔", "依認證畫師預覽或執行歸檔；execute_move=false 僅預覽。", True, Organize, {"execute_move": False, "min_confidence": 0.25}),
    Tool("gallery_clean_duplicates", "清除重複與相似圖片", "比對完全重複（SHA-256 + 位元組驗證）或靜態圖視覺相似（dHash/pHash + SSIM/MAE）。動畫只參與完全重複比對。視覺比對可能漏辨細小差分，先檢視預覽；execute_delete=false 僅預覽，true 重新掃描後清除匹配副本。依像素數、PNG、檔案大小等排序保留，不保證藝術品質；繼承核心與人工標籤，刪除前備份資料庫並移至資源回收筒。", True, CleanDuplicates, {"mode": "both", "scope": "same_folder", "folder": "__ALL__", "execute_delete": False}, background=True),
    Tool("gallery_scan_library", "掃描圖庫", "掃描目前設定的圖庫並同步索引；保留人工標籤，完整掃描才標記遺失。回傳 run_id，使用 gallery_get_tool_run 查詢結果。", True, Overview, {}, background=True),
    Tool("gallery_search_images", "搜尋圖片", "以關鍵字、標籤、資料夾及 AI 狀態分頁查詢圖片，取得後續操作所需的 image_id。", False, ImageSearch, {"q": "風景", "page_size": 20}),
    Tool("gallery_get_image_details", "圖片詳細資料", "取得圖片標籤、處理狀態、遺失狀態及圖庫內路徑；tag_limit 限制回傳標籤數。", False, ImageDetails, {"image_id": 1}),
    Tool("gallery_update_image_tags", "編輯核心標籤", "對指定圖片新增畫師、角色、作品或來源標籤，記為模型來源；不能新增一般描述、分級、年份或主題，也不能覆蓋或移除人工標籤。整批 ID 必須存在。", True, EditTags, {"image_ids": [1], "add_tags": [{"name": "artist_name", "category": "artist"}]}),
    Tool("gallery_analyze_images", "本機 AI 打標", "對指定圖片執行本機視覺打標，保留人工標籤。首次使用可能下載模型；預設略過相同檔案與模型版本已完成項目。回傳 run_id。", True, AnalyzeImages, {"image_ids": [1], "skip_completed": True}, network=True, background=True),
    Tool("gallery_move_images", "搬移指定圖片", "預覽或搬移明確指定的圖片至圖庫內資料夾，使用可復原檔案日誌。execute_move=false 預覽，true 才搬移。回傳 run_id。", True, MoveImages, {"image_ids": [1], "target_folder": "artist_name", "execute_move": False}, background=True),
    Tool("gallery_recover_file_operations", "復原中斷檔案操作", "檢查並修復尚未完成的檔案操作日誌；不會撤銷已完成的正常搬移。回傳 run_id。", True, Overview, {}, background=True),
    Tool("gallery_list_rename_batches", "可還原改名批次", "列出最近可還原的已完成改名批次，供 gallery_undo_rename 使用。", False, Overview, {}),
    Tool("gallery_undo_rename", "還原已完成改名", "依 gallery_list_rename_batches 回傳的 batch_id 還原改名；遇到檔案衝突會停止而不覆寫。回傳 run_id。", True, UndoRename, {"batch_id": "replace_with_existing_batch_id"}, background=True),
    Tool("gallery_get_jobs", "查詢圖庫任務", "取得跨程序目前作業、進度及最近任務。輪詢不建立額外工具執行紀錄。", False, JobList, {"limit": 10}, audit=False),
    Tool("gallery_get_tool_run", "查詢工具執行結果", "依 run_id 取得背景工具狀態、job_id、結果與錯誤；執行中可每 1–2 秒查詢。輪詢不建立額外紀錄。", False, ToolRun, {"run_id": "0" * 32}, audit=False),
    Tool("gallery_cancel_job", "要求停止圖庫作業", "使用啟動回應非 null 的 job_id 或 gallery_get_jobs 的 active.id 合作式停止目前圖庫作業，等待安全步驟結束；不可傳 run_id 或桌面查證工作 ID。", True, CancelJob, {"job_id": "0" * 32}, exclusive=False),
    Tool("gallery_cancel_agent_job", "要求停止桌面查證工作", "以 agent_job_id 停止 Antigravity 桌面查證工作，拒絕後續提交並在安全檢查點停止；不強制中止已開始的網路請求或檔案操作。此 ID 是工作輸入的 job_id，不是工具 run_id 或共用圖庫作業 job_id。", True, CancelAgentJob, {"agent_job_id": "0" * 32}, exclusive=False),
    Tool("gallery_list_mcp_servers", "列出 MCP 管理卡片", "讀取目前圖庫 MCP 管理中的所有 MCP 卡片設定（含內建 Antigravity 主圖庫、背景查證 Sidecar 與自訂串連項目）。", False, Overview, {}),
    Tool("gallery_save_mcp_server", "新增或調整 MCP 卡片", "新增自訂 MCP 連線卡片，或調整既有 MCP 卡片的啟用、唯讀模式、stdio/HTTP/SSE 連線參數、停用工具與擴充設定；可選 sync_client 同步至 Antigravity。", True, SaveMcpServer, {"server_id": "gallery", "enabled": True, "read_only": False}),
    Tool("gallery_delete_mcp_server", "刪除自訂 MCP 卡片", "依 server_id 刪除自訂的 MCP 連線卡片（內建 gallery 與 gallerypub 不可刪除）；可選 sync_client 同步更新 Antigravity。", True, DeleteMcpServer, {"server_id": "custom_mcp", "sync_client": False}),
    Tool("gallery_test_mcp_server", "測試 MCP 卡片連線", "對指定 server_id 的 MCP 卡片執行實際握手診斷（stdio 啟動 initialize/tools/list 或 HTTP/SSE 端點測試）。", False, McpServerId, {"server_id": "gallery"}, network=True),
]
CATALOG = {tool.name: tool for tool in _tools}
TOOLS = {tool.name: (tool.label, tool.writes) for tool in _tools}


def get_tool(name):
    if name not in CATALOG:
        raise KeyError("未知的 MCP 工具")
    return CATALOG[name]


def catalog():
    from integration_settings import allowed, load
    from prompt_store import details
    config = load()
    return [{**tool.protocol(), "label": tool.label, "writes": tool.writes, "network": tool.network,
             "enabled": allowed(tool.name, config), "example": tool.example,
             "background": tool.background, "exclusive": tool.exclusive, **details(tool)} for tool in _tools]


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
