from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

# One line per Worker capability pack. This is routing context only; it never
# grants permissions and never executes a tool.
CAPABILITY_CATALOG: Tuple[Tuple[str, str], ...] = (
    ("web_research", "搜索/抓取静态网页、新闻、资料与图片（web_search / web_fetch）"),
    ("doc_office", "xlsx/csv/pdf/docx 读取、表格加工、PDF 操作、生成文档"),
    ("macos_office", "备忘录/日历/提醒事项/邮件/当前浏览器标签读取"),
    ("browser_auto", "打开/操作网页、动态页面、填表、登录后台（isolated 默认；live 需审批）"),
    ("dev_ops", "shell、目录探查、本地批处理"),
    ("media_studio", "图片/音视频素材处理"),
)

_CATALOG_TITLE = "【能力目录（Router 选工具参考，不构成授权）】"
_HINT_TITLE = "【意图提示（本地规则，仅供路由，不构成授权）】"

_BROWSER_VERBS = re.compile(
    r"(试|用|打开|访问|操作|控制|自动化|抓取|爬取|登录|点击|填写|填表|提交|截图)"
    r".{0,10}(浏览器|网页|网站|页面|后台|表单|链接)",
    re.IGNORECASE,
)
_BROWSER_NOUN = re.compile(
    r"(浏览器(能力|功能|自动化)|网页自动化|动态页面|playwright|\bcdp\b|chrome\s*自动化)",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_DOMAIN_RE = re.compile(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b", re.IGNORECASE)
_BROWSER_PAGE_ACTION = re.compile(
    r"(网页|页面|网站).{0,12}(抓|提取|下载|保存|存|复制|导出)",
    re.IGNORECASE,
)
_DOC_KEYWORDS = re.compile(
    r"(表格|excel|csv|pdf|word|文档|重命名|核对|合并|拆分|报表|xlsx|xlsm|docx|markdown)",
    re.IGNORECASE,
)
_DOC_EXTENSIONS = {".xlsx", ".xls", ".xlsm", ".csv", ".pdf", ".docx", ".md", ".markdown"}
_MACOS_KEYWORDS = re.compile(
    r"(备忘录|笔记|日历|日程|提醒|待办|邮件|notes?|calendar|reminders?|mail)",
    re.IGNORECASE,
)
_WEB_KEYWORDS = re.compile(
    r"(搜索|搜一下|搜图|查一下|查查|新闻|最新|资料|百科|汇率|天气|壁纸|图片搜索|google|bing)",
    re.IGNORECASE,
)
_DEV_KEYWORDS = re.compile(
    r"(\bshell\b|终端|命令行|脚本|批处理|目录|文件列表|\bgit\b|\bgrep\b|python\s+-|npm\s+|pytest)",
    re.IGNORECASE,
)
_MEDIA_KEYWORDS = re.compile(
    r"(视频|音频|剪辑|素材|照片|图片|壁纸|转码|ffmpeg)",
    re.IGNORECASE,
)

_SIGNAL_PATTERNS = (
    ("browser_auto", _BROWSER_VERBS),
    ("browser_auto", _BROWSER_NOUN),
    ("doc_office", _DOC_KEYWORDS),
    ("macos_office", _MACOS_KEYWORDS),
    ("web_research", _WEB_KEYWORDS),
    ("dev_ops", _DEV_KEYWORDS),
    ("media_studio", _MEDIA_KEYWORDS),
)


@dataclass(frozen=True)
class IntentHint:
    intent: str
    candidate_packs: Tuple[str, ...]
    signals: Tuple[str, ...]
    confidence: float

    def render(self) -> str:
        if not self.candidate_packs:
            return ""
        confidence = "高" if self.confidence >= 0.75 else ("中" if self.confidence >= 0.45 else "低")
        lines = [
            _HINT_TITLE,
            f"候选：{self.intent}（{confidence}）"
            + (
                "；备选：" + "、".join(self.candidate_packs[1:])
                if len(self.candidate_packs) > 1
                else ""
            ),
        ]
        if self.signals:
            lines.append("命中：" + "、".join(self.signals[:6]))
        lines.append("按业务目标选择能力包；不要仅凭此行直接执行工具或扩权。")
        return "\n".join(lines)


def build_capability_catalog() -> str:
    lines = [_CATALOG_TITLE]
    for pack, description in CAPABILITY_CATALOG:
        lines.append(f"- {pack}：{description}")
    return "\n".join(lines)


def _attachment_extensions(attachments: Iterable[str]) -> List[str]:
    extensions: List[str] = []
    for raw in attachments or []:
        ext = Path(str(raw)).suffix.lower()
        if ext in _DOC_EXTENSIONS and ext not in extensions:
            extensions.append(ext)
    return extensions


def resolve_intent(
    user_text: str,
    *,
    attachments: Sequence[str] = (),
    max_candidates: int = 3,
) -> IntentHint:
    """Cheap deterministic pack hints. No tools, no mandate, no network."""
    text = str(user_text or "").strip()
    lowered = text.lower()
    signals: List[str] = []
    scores: dict[str, float] = {}

    def add(pack: str, weight: float, signal: str) -> None:
        scores[pack] = scores.get(pack, 0.0) + weight
        if signal and signal not in signals:
            signals.append(signal)

    for pack, pattern in _SIGNAL_PATTERNS:
        match = pattern.search(lowered)
        if match:
            add(pack, 2.0 if pack in ("browser_auto", "doc_office") else 1.2, match.group(0)[:24])

    page_action = _BROWSER_PAGE_ACTION.search(text)
    if page_action:
        add("browser_auto", 2.2, page_action.group(0)[:24])
        if _DOC_KEYWORDS.search(text) or re.search(r"\.(xlsx|csv|md)\b", lowered):
            add("doc_office", 1.0, "网页数据落表")

    url_match = _URL_RE.search(text)
    if not url_match and re.search(r"(打开|访问|登录)", text):
        url_match = _DOMAIN_RE.search(text)
        if url_match and "." in url_match.group(0) and not re.search(
            r"\.(xlsx|xls|csv|pdf|docx|md)$", url_match.group(0), re.IGNORECASE
        ):
            add("browser_auto", 1.5, "网址")
    if url_match:
        add("browser_auto", 1.5, "URL")
        if re.search(r"(打开|访问|操作|抓取|截图|登录|填写|点击)", text):
            add("browser_auto", 1.0, "URL+操作")
        elif re.search(r"(总结|摘要|读一下|看看)", text):
            add("web_research", 0.8, "URL+阅读")

    extensions = _attachment_extensions(attachments)
    if extensions:
        add("doc_office", 2.5, "附件 " + " ".join(extensions))
    ext_match = re.search(r"\.(xlsx|xlsm|xls|csv|pdf|docx|md)\b", lowered)
    if ext_match:
        add("doc_office", 2.0, "." + ext_match.group(1))

    # Browser verbs describing a local browser (tabs) without navigation are
    # closer to macos_browser_tabs; browser_auto needs an action/page target.
    if "浏览器" in lowered and not _BROWSER_VERBS.search(lowered) and not url_match:
        scores["browser_auto"] = max(0.0, scores.get("browser_auto", 0.0) - 1.0)

    ranking = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    ranking = [(pack, score) for pack, score in ranking if score >= 1.0][:max_candidates]
    if not ranking:
        return IntentHint(intent="", candidate_packs=(), signals=(), confidence=0.0)

    top_pack, top_score = ranking[0]
    confidence = min(0.95, 0.45 + top_score / 10.0)
    if len(ranking) > 1 and ranking[1][1] >= top_score * 0.8:
        confidence = min(confidence, 0.62)
    return IntentHint(
        intent=top_pack,
        candidate_packs=tuple(pack for pack, _ in ranking),
        signals=tuple(signals[:6]),
        confidence=round(confidence, 2),
    )


def build_intent_hint(
    user_text: str,
    *,
    attachments: Sequence[str] = (),
) -> str:
    return resolve_intent(user_text, attachments=attachments).render()


__all__ = [
    "CAPABILITY_CATALOG",
    "IntentHint",
    "build_capability_catalog",
    "build_intent_hint",
    "resolve_intent",
]
