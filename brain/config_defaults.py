from __future__ import annotations

DEFAULT_BLOCKED_APPS = [
    "1Password",
    "Bitwarden",
    "KeePass",
    "Keychain Access",
    "钥匙串访问",
    "微信",
    "WeChat",
]

DEFAULT_BLOCKED_TITLE_KEYWORDS = [
    "Incognito",
    "InPrivate",
    "Private Browsing",
    "无痕",
    "网银",
    "支付宝",
    "Alipay",
    "Bank",
    "Password",
    "密码",
]


DEFAULT_SAFE_SHELL_COMMANDS = [
    "pwd",
    "ls",
    "git status",
    "git log",
    "git diff",
    "which",
    "whoami",
    "uname",
    "date",
    "echo",
    "head",
    "tail",
    "wc",
    "cat",
]

DEFAULT_BLOCKED_SHELL_PATTERNS = [
    r"rm\s+-[a-zA-Z]*r[a-zA-Z]*f?\s+(/|\*|~)",
    r"rm\s+-[a-zA-Z]*f[a-zA-Z]*r\s+(/|\*|~)",
    r"\bmkfs(\.[a-z0-9]+)?\b",
    r"\bdd\s+if=",
    r"\bsudo\b",
    r"\b(shutdown|reboot|halt|poweroff)\b",
    r"\bdiskutil\s+(erase|partition|zeroDisk)",
    r"\bprintenv\b",
    r"\benv\b\s*$",
    r"config\.yaml",
    r"\.env\b",
    r":\(\)\s*\{",
    r">\s*/dev/sd",
    r"chmod\s+-R\s+777\s+/",
]
