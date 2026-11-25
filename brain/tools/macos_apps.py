from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from brain.config import DEFAULT_BLOCKED_TITLE_KEYWORDS, AppConfig
from brain.tools.base import Tool, ToolExecutionContext, ToolResult
from brain.utils import truncate_head_tail
from protocol.models import RiskLevel

logger = logging.getLogger("qimingxing.tools.macos_apps")

OsaScriptRunner = Callable[
    [str, Sequence[str], float], Awaitable[Tuple[int, str, str]]
]

_SENSITIVE_TAB_DOMAINS_AND_KEYWORDS = (
    "1password",
    "bitwarden",
    "lastpass",
    "dashlane",
    "keepass",
    "keychain",
    "icbc",
    "cmbchina",
    "ccb.com",
    "boc.cn",
    "abchina",
    "bankofchina",
    "bank",
    "alipay",
    "paypal",
    "chase.com",
    "citibank",
    "wellsfargo",
    "网银",
    "支付宝",
    "银行",
    "密码",
    "无痕",
    "incognito",
    "inprivate",
    "private browsing",
    "password",
    "vault",
)


def _format_automation_error(stderr: str, app_hint: str) -> str:
    low = (stderr or "").lower()
    if (
        "-1743" in stderr
        or "not authorized to send apple events" in low
        or "不允许发送 apple 事件" in stderr
        or "not permitted" in low
    ):
        return (
            f"未获得控制「{app_hint}」的 macOS 自动化权限（错误码 -1743）。\n"
            "请前往「系统设置 → 隐私与安全性 → 自动化」，找到「Qimingxing.app」"
            f"（开发期为当前终端/Python）并勾选「{app_hint}」后重试。"
        )
    return stderr.strip() or f"调用「{app_hint}」失败。"


async def run_osascript(
    jxa_script: str,
    argv: Sequence[str] = (),
    *,
    timeout_sec: float = 5.0,
    runner: Optional[OsaScriptRunner] = None,
) -> Tuple[int, str, str]:
    """Execute JXA via `osascript -l JavaScript - *argv` with stdin script and zero shell interpolation."""
    clean_args = [str(a) for a in argv]
    if runner is not None:
        return await runner(jxa_script, clean_args, timeout_sec)

    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript",
            "-l",
            "JavaScript",
            "-",
            *clean_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return 127, "", "当前系统未找到 osascript（仅 macOS 可用）。"

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(input=jxa_script.encode("utf-8")),
            timeout=max(0.5, float(timeout_sec)),
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await proc.wait()
        except Exception:
            pass
        return 124, "", f"osascript 执行超时（超过 {timeout_sec:.1f} 秒）。"

    return (
        int(proc.returncode or 0),
        stdout_b.decode("utf-8", errors="replace").strip(),
        stderr_b.decode("utf-8", errors="replace").strip(),
    )


def _is_sensitive_browser_tab(
    title: str,
    url: str,
    config: Optional[AppConfig] = None,
) -> bool:
    """Return True if a browser tab matches privacy blacklists (banking, password managers, private browsing)."""
    t_low = (title or "").lower()
    u_low = (url or "").lower()
    host = (urlparse(url or "").hostname or "").lower()
    combined = f"{t_low} {u_low} {host}"

    keywords: List[str] = list(_SENSITIVE_TAB_DOMAINS_AND_KEYWORDS)
    if config is not None:
        keywords.extend(config.privacy.blocked_title_keywords or [])
        keywords.extend(config.privacy.blocked_apps or [])
    else:
        keywords.extend(DEFAULT_BLOCKED_TITLE_KEYWORDS)

    for kw in keywords:
        k = str(kw or "").strip().lower()
        if k and k in combined:
            return True
    return False


def filter_browser_tabs(
    tabs: Sequence[Dict[str, Any]],
    config: Optional[AppConfig] = None,
) -> Tuple[List[Dict[str, str]], int]:
    """Filter out privacy-sensitive browser tabs before returning any tab info to LLM."""
    kept: List[Dict[str, str]] = []
    filtered_count = 0
    for tab in tabs:
        browser = str(tab.get("browser") or "")
        title = str(tab.get("title") or "")
        url = str(tab.get("url") or "")
        if _is_sensitive_browser_tab(title, url, config=config):
            filtered_count += 1
            continue
        kept.append({"browser": browser, "title": title, "url": url})
    return kept, filtered_count


# ---------------------------------------------------------------------------
# JXA templates (static scripts reading dynamic inputs strictly via `argv`)
# ---------------------------------------------------------------------------

_JXA_NOTES_SEARCH = """
function run(argv) {
  var query = (argv[0] || "").toLowerCase();
  var limit = parseInt(argv[1] || "10", 10) || 10;
  var Notes = Application("Notes");
  var allNotes = Notes.notes();
  var out = [];
  for (var i = 0; i < allNotes.length; i++) {
    var n = allNotes[i];
    var name = String(n.name() || "");
    var body = String(n.plaintext() || "");
    if (!query || name.toLowerCase().indexOf(query) >= 0 || body.toLowerCase().indexOf(query) >= 0) {
      out.push({name: name, body: body.slice(0, 1200)});
      if (out.length >= limit) break;
    }
  }
  return JSON.stringify(out);
}
""".strip()

_JXA_NOTES_CREATE = """
function run(argv) {
  var title = argv[0] || "启明星笔记";
  var body = argv[1] || "";
  var folderName = argv[2] || "";
  var Notes = Application("Notes");
  var htmlBody = "<h1>" + title.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;") + "</h1>"
    + "<p>" + body.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/\\n/g,"<br>") + "</p>";
  var note = Notes.Note({body: htmlBody});
  if (folderName) {
    var matches = Notes.folders.whose({name: folderName})();
    if (matches.length > 0) {
      matches[0].notes.push(note);
      return JSON.stringify({ok: true, title: title, folder: folderName});
    }
  }
  Notes.defaultAccount().defaultFolder().notes.push(note);
  return JSON.stringify({ok: true, title: title, folder: "default"});
}
""".strip()

_JXA_CALENDAR_LIST = """
function run(argv) {
  var days = parseInt(argv[0] || "7", 10) || 7;
  var calFilter = argv[1] || "";
  var Calendar = Application("Calendar");
  var now = new Date();
  var end = new Date(now.getTime() + days * 86400 * 1000);
  var out = [];
  var cals = calFilter ? Calendar.calendars.whose({name: calFilter})() : Calendar.calendars();
  for (var i = 0; i < cals.length; i++) {
    var c = cals[i];
    var cname = String(c.name() || "");
    var evs = c.events.whose({
      _and: [{startDate: {_greaterThanEquals: now}}, {startDate: {_lessThanEquals: end}}]
    })();
    for (var j = 0; j < evs.length; j++) {
      var e = evs[j];
      out.push({
        calendar: cname,
        summary: String(e.summary() || ""),
        start: e.startDate() ? e.startDate().toISOString() : "",
        end: e.endDate() ? e.endDate().toISOString() : "",
        location: String(e.location() || "")
      });
      if (out.length >= 40) break;
    }
    if (out.length >= 40) break;
  }
  return JSON.stringify(out);
}
""".strip()

_JXA_CALENDAR_CREATE = """
function run(argv) {
  var title = argv[0] || "";
  var startIso = argv[1] || "";
  var endIso = argv[2] || "";
  var calName = argv[3] || "";
  var desc = argv[4] || "";
  var Calendar = Application("Calendar");
  var startDate = new Date(startIso);
  var endDate = endIso ? new Date(endIso) : new Date(startDate.getTime() + 3600 * 1000);
  var targetCal = null;
  if (calName) {
    var matched = Calendar.calendars.whose({name: calName})();
    if (matched.length > 0) targetCal = matched[0];
  }
  if (!targetCal) {
    var allCals = Calendar.calendars();
    for (var i = 0; i < allCals.length; i++) {
      if (allCals[i].writable()) { targetCal = allCals[i]; break; }
    }
  }
  if (!targetCal) throw new Error("未找到可写入的日历");
  var ev = Calendar.Event({
    summary: title,
    startDate: startDate,
    endDate: endDate,
    description: desc
  });
  targetCal.events.push(ev);
  return JSON.stringify({ok: true, calendar: String(targetCal.name()), summary: title});
}
""".strip()

_JXA_REMINDERS_LIST = """
function run(argv) {
  var listName = argv[0] || "";
  var limit = parseInt(argv[1] || "20", 10) || 20;
  var Reminders = Application("Reminders");
  var lists = listName ? Reminders.lists.whose({name: listName})() : Reminders.lists();
  var out = [];
  for (var i = 0; i < lists.length; i++) {
    var lst = lists[i];
    var lname = String(lst.name() || "");
    var items = lst.reminders.whose({completed: false})();
    for (var j = 0; j < items.length; j++) {
      var r = items[j];
      out.push({
        list: lname,
        name: String(r.name() || ""),
        due: r.dueDate() ? r.dueDate().toISOString() : "",
        body: String(r.body() || "").slice(0, 200)
      });
      if (out.length >= limit) break;
    }
    if (out.length >= limit) break;
  }
  return JSON.stringify(out);
}
""".strip()

_JXA_REMINDERS_MUTATE = """
function run(argv) {
  var action = argv[0] || "create";
  var title = argv[1] || "";
  var listName = argv[2] || "";
  var dueIso = argv[3] || "";
  var body = argv[4] || "";
  var Reminders = Application("Reminders");
  if (action === "complete") {
    var lists = listName ? Reminders.lists.whose({name: listName})() : Reminders.lists();
    for (var i = 0; i < lists.length; i++) {
      var items = lists[i].reminders.whose({completed: false, name: title})();
      if (items.length > 0) {
        items[0].completed = true;
        return JSON.stringify({ok: true, action: "complete", title: title});
      }
    }
    return JSON.stringify({ok: false, error: "未找到匹配的未完成提醒事项"});
  }
  var targetList = listName ? Reminders.lists.whose({name: listName})()[0] : Reminders.defaultList();
  var props = {name: title, body: body};
  if (dueIso) props.dueDate = new Date(dueIso);
  var rem = Reminders.Reminder(props);
  targetList.reminders.push(rem);
  return JSON.stringify({ok: true, action: "create", title: title, list: String(targetList.name())});
}
""".strip()

_JXA_BROWSER_TABS = """
function run(argv) {
  var target = (argv[0] || "all").toLowerCase();
  var se = Application("System Events");
  var procs = se.processes.whose({backgroundOnly: false}).name();
  var out = [];
  function hasProc(name) {
    for (var i = 0; i < procs.length; i++) {
      if (String(procs[i]) === name) return true;
    }
    return false;
  }
  if ((target === "all" || target === "safari") && hasProc("Safari")) {
    try {
      var Safari = Application("Safari");
      var wins = Safari.windows();
      for (var w = 0; w < wins.length; w++) {
        var tabs = wins[w].tabs();
        for (var t = 0; t < tabs.length; t++) {
          out.push({browser: "Safari", title: String(tabs[t].name() || ""), url: String(tabs[t].url() || "")});
        }
      }
    } catch (e) {}
  }
  if ((target === "all" || target === "chrome") && hasProc("Google Chrome")) {
    try {
      var Chrome = Application("Google Chrome");
      var cwins = Chrome.windows();
      for (var w2 = 0; w2 < cwins.length; w2++) {
        if (String(cwins[w2].mode() || "") === "incognito") continue;
        var ctabs = cwins[w2].tabs();
        for (var t2 = 0; t2 < ctabs.length; t2++) {
          out.push({browser: "Chrome", title: String(ctabs[t2].title() || ""), url: String(ctabs[t2].url() || "")});
        }
      }
    } catch (e) {}
  }
  if ((target === "all" || target === "arc") && hasProc("Arc")) {
    try {
      var Arc = Application("Arc");
      var awins = Arc.windows();
      for (var w3 = 0; w3 < awins.length; w3++) {
        if (awins[w3].incognito && awins[w3].incognito()) continue;
        var atabs = awins[w3].tabs();
        for (var t3 = 0; t3 < atabs.length; t3++) {
          out.push({browser: "Arc", title: String(atabs[t3].title() || ""), url: String(atabs[t3].url() || "")});
        }
      }
    } catch (e) {}
  }
  return JSON.stringify(out);
}
""".strip()

_JXA_MAIL_SEARCH = """
function run(argv) {
  var query = (argv[0] || "").toLowerCase();
  var limit = parseInt(argv[1] || "10", 10) || 10;
  var Mail = Application("Mail");
  var msgs = Mail.inbox.messages();
  var out = [];
  var scanLimit = Math.min(msgs.length, 60);
  for (var i = 0; i < scanLimit; i++) {
    var m = msgs[i];
    var subj = String(m.subject() || "");
    var sender = String(m.sender() || "");
    var content = String(m.content() || "").slice(0, 400);
    if (!query || subj.toLowerCase().indexOf(query) >= 0 || sender.toLowerCase().indexOf(query) >= 0 || content.toLowerCase().indexOf(query) >= 0) {
      out.push({
        subject: subj,
        sender: sender,
        date: m.dateReceived() ? m.dateReceived().toISOString() : "",
        snippet: content
      });
      if (out.length >= limit) break;
    }
  }
  return JSON.stringify(out);
}
""".strip()

# Draft-only JXA: opens compose window with visible=true; strictly NO transmit/deliver method is invoked.
_JXA_MAIL_DRAFT = """
function run(argv) {
  var toAddr = argv[0] || "";
  var subject = argv[1] || "";
  var body = argv[2] || "";
  var ccAddr = argv[3] || "";
  var Mail = Application("Mail");
  var msg = Mail.OutgoingMessage({
    subject: subject,
    content: body,
    visible: true
  });
  Mail.outgoingMessages.push(msg);
  if (toAddr) {
    msg.toRecipients.push(Mail.Recipient({address: toAddr}));
  }
  if (ccAddr) {
    msg.ccRecipients.push(Mail.Recipient({address: ccAddr}));
  }
  Mail.activate();
  return JSON.stringify({ok: true, draft_created: true, to: toAddr, subject: subject});
}
""".strip()


class _MacOSJxaToolBase(Tool):
    def __init__(self, *, runner: Optional[OsaScriptRunner] = None) -> None:
        self._runner = runner

    async def _run_jxa(
        self,
        script: str,
        argv: Sequence[str],
        *,
        app_hint: str,
        timeout_sec: float = 5.0,
    ) -> Tuple[bool, str]:
        rc, out, err = await run_osascript(
            script, argv, timeout_sec=timeout_sec, runner=self._runner
        )
        if rc != 0:
            return False, _format_automation_error(err or out, app_hint)
        return True, out


class MacOSNotesSearchTool(_MacOSJxaToolBase):
    name = "macos_notes_search"
    description = "检索 macOS 备忘录（Apple Notes）中的笔记标题与正文（只读，4000 字符截断脱敏）。"
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词，留空返回近期笔记"},
            "limit": {"type": "integer", "description": "最多返回条数，默认 10"},
        },
    }
    default_risk: RiskLevel = "safe"

    def classify_risk(self, arguments: Dict[str, Any]) -> RiskLevel:
        return "safe"

    def format_status_label(self, arguments: Dict[str, Any]) -> str:
        return "正在检索 macOS 备忘录…"

    async def execute(
        self, arguments: Dict[str, Any], ctx: ToolExecutionContext
    ) -> ToolResult:
        query = str(arguments.get("query") or "").strip()
        limit = max(1, min(30, int(arguments.get("limit") or 10)))
        ok, raw = await self._run_jxa(
            _JXA_NOTES_SEARCH, [query, str(limit)], app_hint="备忘录 (Notes)"
        )
        if not ok:
            return ToolResult(ok=False, output=raw, summary="备忘录检索失败", status="error")
        try:
            items = json.loads(raw or "[]")
        except json.JSONDecodeError:
            items = []
        if not items:
            return ToolResult(
                ok=True, output="未找到匹配的备忘录。", summary="无匹配备忘录", status="ok"
            )
        lines = []
        for idx, it in enumerate(items, 1):
            lines.append(f"{idx}. **{it.get('name', '无标题')}**\n{it.get('body', '')}")
        text = truncate_head_tail(ctx.config.redact_secrets("\n\n".join(lines)), 4000)
        return ToolResult(
            ok=True,
            output=text,
            summary=f"检索到 {len(items)} 条备忘录",
            status="ok",
            metadata={"count": len(items)},
        )


class MacOSNotesCreateTool(_MacOSJxaToolBase):
    name = "macos_notes_create"
    description = "在 macOS 备忘录（Apple Notes）中新建一条笔记（需经审批确认）。"
    parameters_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "笔记标题"},
            "body": {"type": "string", "description": "笔记正文内容"},
            "folder": {"type": "string", "description": "可选文件夹名称"},
        },
        "required": ["title", "body"],
    }
    default_risk: RiskLevel = "confirm"

    def classify_risk(self, arguments: Dict[str, Any]) -> RiskLevel:
        return "confirm"

    def format_status_label(self, arguments: Dict[str, Any]) -> str:
        return f"正在写入备忘录：{str(arguments.get('title') or '')[:30]}…"

    def format_approval_summary(self, arguments: Dict[str, Any]) -> str:
        title = str(arguments.get("title") or "未命名笔记").strip()
        return f"新建 macOS 备忘录「{title}」"

    def format_approval_details(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        body = str(arguments.get("body") or "")
        return {
            "app": "Notes",
            "title": str(arguments.get("title") or ""),
            "folder": str(arguments.get("folder") or "默认文件夹"),
            "body_preview": body[:240] + ("..." if len(body) > 240 else ""),
        }

    async def execute(
        self, arguments: Dict[str, Any], ctx: ToolExecutionContext
    ) -> ToolResult:
        title = str(arguments.get("title") or "").strip()
        body = str(arguments.get("body") or "")
        folder = str(arguments.get("folder") or "").strip()
        if not title:
            return ToolResult(
                ok=False, output="请提供笔记标题 title。", summary="参数缺失", status="error"
            )
        ok, raw = await self._run_jxa(
            _JXA_NOTES_CREATE, [title, body, folder], app_hint="备忘录 (Notes)"
        )
        if not ok:
            return ToolResult(ok=False, output=raw, summary="写入备忘录失败", status="error")
        return ToolResult(
            ok=True,
            output=f"已在 macOS 备忘录成功创建笔记「{title}」。",
            summary=f"已创建备忘录：{title}",
            status="ok",
        )


class MacOSCalendarListTool(_MacOSJxaToolBase):
    name = "macos_calendar_list"
    description = "查询 macOS 日历（Calendar）未来 N 天内的日程安排（默认 7 天，只读）。"
    parameters_schema = {
        "type": "object",
        "properties": {
            "days": {"type": "integer", "description": "未来天数，默认 7"},
            "calendar_name": {"type": "string", "description": "可选，指定日历名称"},
        },
    }
    default_risk: RiskLevel = "safe"

    def classify_risk(self, arguments: Dict[str, Any]) -> RiskLevel:
        return "safe"

    def format_status_label(self, arguments: Dict[str, Any]) -> str:
        return "正在查询 macOS 日历日程…"

    async def execute(
        self, arguments: Dict[str, Any], ctx: ToolExecutionContext
    ) -> ToolResult:
        days = max(1, min(30, int(arguments.get("days") or 7)))
        cal_name = str(arguments.get("calendar_name") or "").strip()
        ok, raw = await self._run_jxa(
            _JXA_CALENDAR_LIST, [str(days), cal_name], app_hint="日历 (Calendar)"
        )
        if not ok:
            return ToolResult(ok=False, output=raw, summary="日历查询失败", status="error")
        try:
            items = json.loads(raw or "[]")
        except json.JSONDecodeError:
            items = []
        if not items:
            return ToolResult(
                ok=True,
                output=f"未来 {days} 天内暂无日程安排。",
                summary="未来日程为空",
                status="ok",
            )
        lines = [
            f"- [{e.get('calendar', '')}] {e.get('summary', '')} ({e.get('start', '')} ~ {e.get('end', '')})"
            + (f" @ {e.get('location')}" if e.get("location") else "")
            for e in items
        ]
        text = truncate_head_tail(ctx.config.redact_secrets("\n".join(lines)), 4000)
        return ToolResult(
            ok=True,
            output=text,
            summary=f"查到 {len(items)} 条未来日程",
            status="ok",
            metadata={"count": len(items)},
        )


class MacOSCalendarCreateTool(_MacOSJxaToolBase):
    name = "macos_calendar_create"
    description = "在 macOS 日历（Calendar）中新建日程事件（需经审批确认）。"
    parameters_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "日程标题"},
            "start_iso": {"type": "string", "description": "开始时间 ISO-8601（如 2026-11-26T10:00:00+08:00）"},
            "end_iso": {"type": "string", "description": "结束时间 ISO-8601，留空默认 1 小时"},
            "calendar_name": {"type": "string", "description": "可选，目标日历名称"},
            "description": {"type": "string", "description": "可选，日程备注"},
        },
        "required": ["title", "start_iso"],
    }
    default_risk: RiskLevel = "confirm"

    def classify_risk(self, arguments: Dict[str, Any]) -> RiskLevel:
        return "confirm"

    def format_status_label(self, arguments: Dict[str, Any]) -> str:
        return f"正在创建日历事件：{str(arguments.get('title') or '')[:30]}…"

    def format_approval_summary(self, arguments: Dict[str, Any]) -> str:
        return f"新建日历日程「{arguments.get('title', '')}」（{arguments.get('start_iso', '')}）"

    def format_approval_details(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "app": "Calendar",
            "title": str(arguments.get("title") or ""),
            "start_iso": str(arguments.get("start_iso") or ""),
            "end_iso": str(arguments.get("end_iso") or "默认 +1 小时"),
            "calendar_name": str(arguments.get("calendar_name") or "默认可写日历"),
        }

    async def execute(
        self, arguments: Dict[str, Any], ctx: ToolExecutionContext
    ) -> ToolResult:
        title = str(arguments.get("title") or "").strip()
        start_iso = str(arguments.get("start_iso") or "").strip()
        end_iso = str(arguments.get("end_iso") or "").strip()
        cal_name = str(arguments.get("calendar_name") or "").strip()
        desc = str(arguments.get("description") or "").strip()
        if not title or not start_iso:
            return ToolResult(
                ok=False,
                output="请提供日程标题 title 与开始时间 start_iso。",
                summary="参数缺失",
                status="error",
            )
        ok, raw = await self._run_jxa(
            _JXA_CALENDAR_CREATE,
            [title, start_iso, end_iso, cal_name, desc],
            app_hint="日历 (Calendar)",
        )
        if not ok:
            return ToolResult(ok=False, output=raw, summary="创建日程失败", status="error")
        return ToolResult(
            ok=True,
            output=f"已在 macOS 日历创建日程「{title}」（开始时间: {start_iso}）。",
            summary=f"已创建日程：{title}",
            status="ok",
        )


class MacOSRemindersListTool(_MacOSJxaToolBase):
    name = "macos_reminders_list"
    description = "列出 macOS 提醒事项（Reminders）中的未完成待办（只读）。"
    parameters_schema = {
        "type": "object",
        "properties": {
            "list_name": {"type": "string", "description": "可选，指定提醒列表名称"},
            "limit": {"type": "integer", "description": "最多返回条数，默认 20"},
        },
    }
    default_risk: RiskLevel = "safe"

    def classify_risk(self, arguments: Dict[str, Any]) -> RiskLevel:
        return "safe"

    def format_status_label(self, arguments: Dict[str, Any]) -> str:
        return "正在查询 macOS 提醒事项…"

    async def execute(
        self, arguments: Dict[str, Any], ctx: ToolExecutionContext
    ) -> ToolResult:
        list_name = str(arguments.get("list_name") or "").strip()
        limit = max(1, min(50, int(arguments.get("limit") or 20)))
        ok, raw = await self._run_jxa(
            _JXA_REMINDERS_LIST, [list_name, str(limit)], app_hint="提醒事项 (Reminders)"
        )
        if not ok:
            return ToolResult(ok=False, output=raw, summary="提醒事项查询失败", status="error")
        try:
            items = json.loads(raw or "[]")
        except json.JSONDecodeError:
            items = []
        if not items:
            return ToolResult(
                ok=True, output="当前没有未完成的提醒事项。", summary="待办为空", status="ok"
            )
        lines = [
            f"- [{r.get('list', '')}] {r.get('name', '')}"
            + (f" (截止: {r.get('due')})" if r.get("due") else "")
            for r in items
        ]
        text = truncate_head_tail(ctx.config.redact_secrets("\n".join(lines)), 4000)
        return ToolResult(
            ok=True,
            output=text,
            summary=f"共 {len(items)} 条未完成待办",
            status="ok",
            metadata={"count": len(items)},
        )


class MacOSRemindersCreateTool(_MacOSJxaToolBase):
    name = "macos_reminders_create"
    description = "在 macOS 提醒事项（Reminders）中新建待办或勾选完成已有待办（需经审批确认）。"
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "complete"],
                "description": "操作类型：create（新建）或 complete（标记完成）",
            },
            "title": {"type": "string", "description": "提醒事项标题"},
            "list_name": {"type": "string", "description": "可选，目标列表名称"},
            "due_iso": {"type": "string", "description": "可选，截止时间 ISO-8601"},
            "notes": {"type": "string", "description": "可选，备注"},
        },
        "required": ["title"],
    }
    default_risk: RiskLevel = "confirm"

    def classify_risk(self, arguments: Dict[str, Any]) -> RiskLevel:
        return "confirm"

    def format_status_label(self, arguments: Dict[str, Any]) -> str:
        return f"正在更新提醒事项：{str(arguments.get('title') or '')[:30]}…"

    def format_approval_summary(self, arguments: Dict[str, Any]) -> str:
        act = str(arguments.get("action") or "create").lower()
        verb = "勾选完成" if act == "complete" else "新建"
        return f"{verb}提醒事项「{arguments.get('title', '')}」"

    def format_approval_details(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "app": "Reminders",
            "action": str(arguments.get("action") or "create"),
            "title": str(arguments.get("title") or ""),
            "list_name": str(arguments.get("list_name") or "默认列表"),
            "due_iso": str(arguments.get("due_iso") or ""),
        }

    async def execute(
        self, arguments: Dict[str, Any], ctx: ToolExecutionContext
    ) -> ToolResult:
        action = str(arguments.get("action") or "create").strip().lower()
        if action not in ("create", "complete"):
            action = "create"
        title = str(arguments.get("title") or "").strip()
        list_name = str(arguments.get("list_name") or "").strip()
        due_iso = str(arguments.get("due_iso") or "").strip()
        notes = str(arguments.get("notes") or "").strip()
        if not title:
            return ToolResult(
                ok=False, output="请提供提醒事项标题 title。", summary="参数缺失", status="error"
            )
        ok, raw = await self._run_jxa(
            _JXA_REMINDERS_MUTATE,
            [action, title, list_name, due_iso, notes],
            app_hint="提醒事项 (Reminders)",
        )
        if not ok:
            return ToolResult(ok=False, output=raw, summary="操作提醒事项失败", status="error")
        return ToolResult(
            ok=True,
            output=f"提醒事项「{title}」已执行 {action}。",
            summary=f"提醒事项 {action}: {title}",
            status="ok",
        )


class MacOSBrowserTabsTool(_MacOSJxaToolBase):
    name = "macos_browser_tabs"
    description = (
        "读取当前打开的 Safari / Chrome / Arc 浏览器标签页标题与 URL；"
        "强制过滤网银、密码管理及隐私模式标签页，绝不将隐私页面送入大模型。"
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "browser": {
                "type": "string",
                "enum": ["all", "safari", "chrome", "arc"],
                "description": "目标浏览器，默认 all",
            }
        },
    }
    default_risk: RiskLevel = "safe"

    def classify_risk(self, arguments: Dict[str, Any]) -> RiskLevel:
        return "safe"

    def format_status_label(self, arguments: Dict[str, Any]) -> str:
        return "正在读取浏览器标签页（自动隐私过滤）…"

    async def execute(
        self, arguments: Dict[str, Any], ctx: ToolExecutionContext
    ) -> ToolResult:
        browser = str(arguments.get("browser") or "all").strip().lower()
        if browser not in ("all", "safari", "chrome", "arc"):
            browser = "all"
        ok, raw = await self._run_jxa(
            _JXA_BROWSER_TABS, [browser], app_hint="浏览器 (Safari/Chrome/Arc)"
        )
        if not ok:
            return ToolResult(ok=False, output=raw, summary="读取浏览器标签页失败", status="error")
        try:
            raw_tabs = json.loads(raw or "[]")
        except json.JSONDecodeError:
            raw_tabs = []

        kept, filtered_count = filter_browser_tabs(raw_tabs, config=ctx.config)
        if not kept and filtered_count == 0:
            return ToolResult(
                ok=True,
                output="当前未检测到打开的 Safari / Chrome / Arc 标签页。",
                summary="无打开的标签页",
                status="ok",
                metadata={"count": 0, "filtered_count": 0},
            )
        lines = [
            f"- [{t['browser']}] {t['title']} | {t['url']}" for t in kept
        ]
        if filtered_count > 0:
            lines.append(f"\n（隐私守卫已自动过滤 {filtered_count} 个敏感/隐私标签页）")
        text = truncate_head_tail(ctx.config.redact_secrets("\n".join(lines)), 4000)
        return ToolResult(
            ok=True,
            output=text or f"（仅检测到 {filtered_count} 个敏感标签页，已全部按隐私规则拦截）",
            summary=f"读取 {len(kept)} 个标签页（过滤 {filtered_count} 个隐私项）",
            status="ok",
            metadata={"count": len(kept), "filtered_count": filtered_count},
        )


class MacOSMailSearchTool(_MacOSJxaToolBase):
    name = "macos_mail_search"
    description = "只读检索 macOS 邮件（Mail.app）收件箱近期邮件摘要（4000 字符截断并强制脱敏）。"
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索主题、发件人或正文关键词"},
            "limit": {"type": "integer", "description": "最多返回封数，默认 10"},
        },
    }
    default_risk: RiskLevel = "safe"

    def classify_risk(self, arguments: Dict[str, Any]) -> RiskLevel:
        return "safe"

    def format_status_label(self, arguments: Dict[str, Any]) -> str:
        return "正在检索 macOS 邮件摘要…"

    async def execute(
        self, arguments: Dict[str, Any], ctx: ToolExecutionContext
    ) -> ToolResult:
        query = str(arguments.get("query") or "").strip()
        limit = max(1, min(20, int(arguments.get("limit") or 10)))
        ok, raw = await self._run_jxa(
            _JXA_MAIL_SEARCH, [query, str(limit)], app_hint="邮件 (Mail)"
        )
        if not ok:
            return ToolResult(ok=False, output=raw, summary="邮件检索失败", status="error")
        try:
            items = json.loads(raw or "[]")
        except json.JSONDecodeError:
            items = []
        if not items:
            return ToolResult(
                ok=True, output="收件箱未找到匹配邮件。", summary="无匹配邮件", status="ok"
            )
        lines = [
            f"- 主题: {m.get('subject', '')} | 发件人: {m.get('sender', '')} | 时间: {m.get('date', '')}\n  摘要: {m.get('snippet', '')}"
            for m in items
        ]
        text = truncate_head_tail(ctx.config.redact_secrets("\n".join(lines)), 4000)
        return ToolResult(
            ok=True,
            output=text,
            summary=f"检索到 {len(items)} 封邮件",
            status="ok",
            metadata={"count": len(items)},
        )


class MacOSMailDraftTool(_MacOSJxaToolBase):
    name = "macos_mail_draft"
    description = (
        "在 macOS 邮件（Mail.app）中创建本地可见草稿窗口供用户人工审阅；"
        "严格禁止自动投递外发，需经审批确认。"
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "收件人邮箱"},
            "subject": {"type": "string", "description": "邮件主题"},
            "body": {"type": "string", "description": "邮件正文"},
            "cc": {"type": "string", "description": "可选，抄送邮箱"},
        },
        "required": ["subject", "body"],
    }
    default_risk: RiskLevel = "confirm"

    def classify_risk(self, arguments: Dict[str, Any]) -> RiskLevel:
        return "confirm"

    def format_status_label(self, arguments: Dict[str, Any]) -> str:
        return "正在创建 macOS 邮件本地草稿…"

    def format_approval_summary(self, arguments: Dict[str, Any]) -> str:
        return f"创建本地邮件草稿「{arguments.get('subject', '')}」（仅打开草稿窗，不外发）"

    def format_approval_details(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        body = str(arguments.get("body") or "")
        return {
            "app": "Mail",
            "mode": "draft_only",
            "to": str(arguments.get("to") or ""),
            "cc": str(arguments.get("cc") or ""),
            "subject": str(arguments.get("subject") or ""),
            "body_preview": body[:200] + ("..." if len(body) > 200 else ""),
        }

    async def execute(
        self, arguments: Dict[str, Any], ctx: ToolExecutionContext
    ) -> ToolResult:
        to_addr = str(arguments.get("to") or "").strip()
        subject = str(arguments.get("subject") or "").strip()
        body = str(arguments.get("body") or "")
        cc_addr = str(arguments.get("cc") or "").strip()
        if not subject:
            return ToolResult(
                ok=False, output="请提供邮件主题 subject。", summary="参数缺失", status="error"
            )
        ok, raw = await self._run_jxa(
            _JXA_MAIL_DRAFT, [to_addr, subject, body, cc_addr], app_hint="邮件 (Mail)"
        )
        if not ok:
            return ToolResult(ok=False, output=raw, summary="创建邮件草稿失败", status="error")
        return ToolResult(
            ok=True,
            output=f"已在 Mail.app 中打开本地草稿窗口「{subject}」（仅保存草稿供人工核对，绝未自动外发）。",
            summary=f"已打开邮件草稿：{subject}",
            status="ok",
        )
