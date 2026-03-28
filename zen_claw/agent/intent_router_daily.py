"""Daily assistant intent definitions (declarative).

These intents are loaded into the DeclarativeIntentEngine and handle common
daily-assistant queries without LLM reasoning.
"""

from __future__ import annotations

from zen_claw.agent.intent_router_contracts import IntentToolContract
from zen_claw.agent.intent_router_declarative import DeclarativeIntent, ParamRule

# ── Contracts ──────────────────────────────────────────────────────────────────

_EMAIL_CONTRACT = IntentToolContract(
    intent_name="email_check",
    intent_mode="router_first",
    preferred_tools=["email"],
    allowed_tools={"email"},
    denied_tools={"exec", "spawn", "write_file", "edit_file"},
    allow_constrained_replan=True,
    allow_high_risk_escalation=False,
    response_mode="direct",
    failure_mode="runtime_direct",
)

_CALENDAR_CONTRACT = IntentToolContract(
    intent_name="calendar_check",
    intent_mode="router_first",
    preferred_tools=["calendar"],
    allowed_tools={"calendar"},
    denied_tools={"exec", "spawn", "write_file", "edit_file"},
    allow_constrained_replan=True,
    allow_high_risk_escalation=False,
    response_mode="direct",
    failure_mode="runtime_direct",
)

_REMINDER_CONTRACT = IntentToolContract(
    intent_name="reminder",
    intent_mode="router_first",
    preferred_tools=["cron", "message"],
    allowed_tools={"cron", "message"},
    denied_tools={"exec", "spawn", "write_file", "edit_file"},
    allow_constrained_replan=True,
    allow_high_risk_escalation=False,
    response_mode="direct",
    failure_mode="runtime_direct",
)

_NOTION_QUERY_CONTRACT = IntentToolContract(
    intent_name="notion_query",
    intent_mode="router_first",
    preferred_tools=["notion"],
    allowed_tools={"notion"},
    denied_tools={"exec", "spawn", "write_file", "edit_file"},
    allow_constrained_replan=True,
    allow_high_risk_escalation=False,
    response_mode="direct",
    failure_mode="runtime_direct",
)

_DAILY_WORKFLOW_CONTRACT = IntentToolContract(
    intent_name="daily_workflow",
    intent_mode="hybrid",
    preferred_tools=["email", "calendar", "message"],
    allowed_tools={"email", "calendar", "gdrive", "notion", "cron", "message"},
    denied_tools={"exec", "spawn"},
    allow_constrained_replan=True,
    allow_high_risk_escalation=False,
    response_mode="direct",
    failure_mode="runtime_direct",
)

# ── Intent Definitions ─────────────────────────────────────────────────────────

EMAIL_CHECK = DeclarativeIntent(
    name="email_check",
    description="Check unread emails, search email by sender/subject/date.",
    patterns=[
        # Chinese
        r"(?:未读|新|有(?:没有|几封)?)?邮件",
        r"收件箱",
        r"查(?:看|一下)?邮件",
        r"(?:有|来)新?(?:邮件|mail)(?:了)?(?:吗|没)",
        # English
        r"\b(?:check|read|show|get)\s+(?:my\s+)?(?:email|inbox|mail)\b",
        r"\bunread\s+(?:email|mail|message)s?\b",
        r"\binbox\b",
    ],
    negative_patterns=[
        # Exclude "send email" / "reply email" / "write email" — those need LLM
        r"(?:发|写|回复|转发|send|reply|forward|compose|write|draft)\s*(?:邮件|email|mail)",
    ],
    params={
        "unread_only": ParamRule(pattern=r"(未读|unread)", default=True, type="bool"),
        "limit": ParamRule(pattern=r"(\d+)\s*(?:封|条|个|emails?)", default=5, type="int"),
        "query": ParamRule(pattern=r"(?:搜|找|search|from|关于)\s*[\"']?(.{2,40}?)[\"']?\s*(?:的邮件|mail|$)", default=None),
    },
    static_params={"action": "read_inbox"},
    tool_name="email",
    tool_action="read_inbox",
    contract=_EMAIL_CONTRACT,
    mode="direct",
    failure_message="Email service unavailable. Check IMAP configuration: tools.email in config.yaml.",
)

CALENDAR_CHECK = DeclarativeIntent(
    name="calendar_check",
    description="Check today's/tomorrow's calendar events or find free slots.",
    patterns=[
        # Chinese
        r"(?:今天|明天|后天|本周|下周|这周).*(?:日程|安排|会议|会|行程|活动|schedule)",
        r"(?:有什么|有没有).*(?:会议|会|安排|日程|活动)",
        r"查(?:看|一下)?(?:日程|日历|行程|会议|安排)",
        r"(?:今天|明天|后天).*(?:有空|空闲|free)(?:吗|没)?",
        # English
        r"\b(?:check|show|get|what(?:'s| is))\s+(?:on\s+)?(?:my\s+)?(?:calendar|schedule|agenda|meetings?)\b",
        r"\btoday(?:'s)?\s+(?:schedule|meetings?|agenda|events?)\b",
        r"\btomorrow(?:'s)?\s+(?:schedule|meetings?|agenda|events?)\b",
        r"\b(?:am i|are you)\s+(?:free|available|busy)\b",
    ],
    negative_patterns=[
        # Exclude "create meeting" / "schedule a meeting" — those need LLM
        r"(?:创建|新建|安排一个|schedule\s+a|create|book|set up)\s+(?:会议|meeting|event)",
    ],
    params={
        "provider": ParamRule(default="google"),  # Overridden at runtime from user_profile
    },
    static_params={"action": "list_events"},
    tool_name="calendar",
    tool_action="list_events",
    contract=_CALENDAR_CONTRACT,
    mode="direct",
    failure_message="Calendar service unavailable. Check calendar provider configuration.",
)

REMINDER = DeclarativeIntent(
    name="reminder",
    description="Set a timed reminder.",
    patterns=[
        # Chinese
        r"(?:提醒我|提醒一下|定个提醒|设个提醒|帮我提醒)",
        r"(\d+)\s*(?:分钟|小时|min|hour)后.*(?:提醒|remind)",
        # English
        r"\bremind\s+me\b",
        r"\bset\s+(?:a\s+)?reminder\b",
    ],
    negative_patterns=[],
    params={
        "message": ParamRule(
            pattern=r"(?:提醒我?|remind\s+me\s+(?:to\s+)?)\s*(.{2,100}?)(?:\s*$|\s+(?:at|in|on))",
            default="Reminder",
        ),
    },
    static_params={"action": "add", "deliver": True},
    tool_name="cron",
    tool_action="add",
    contract=_REMINDER_CONTRACT,
    mode="direct",
    failure_message="Reminder service unavailable. Cron tool may not be configured.",
)

NOTION_QUERY = DeclarativeIntent(
    name="notion_query",
    description="Search Notion pages and databases.",
    patterns=[
        # Chinese
        r"(?:在|查|搜|找|看)\s*(?:一下\s*)?notion\s*(?:里|中|上)?",
        r"notion\s*(?:里|中|上).*(?:的|有)",
        r"(?:查|搜|找).*notion",
        # English
        r"\b(?:search|find|look\s+up|check)\s+(?:in\s+)?notion\b",
        r"\bnotion\s+(?:search|find|query|page|database)\b",
    ],
    negative_patterns=[
        # Exclude "create in notion" / "write to notion" — those need LLM
        r"(?:创建|新建|写入|写到|create|write|add)\s*(?:到|to|in)?\s*notion",
    ],
    params={
        "query": ParamRule(
            pattern=r"(?:notion\s*(?:里|中|上)?(?:的|有)?|(?:in\s+)?notion\s*)\s*(.{2,60})",
            default=None,
        ),
    },
    static_params={"action": "search"},
    tool_name="notion",
    tool_action="search",
    contract=_NOTION_QUERY_CONTRACT,
    mode="direct",
    failure_message="Notion service unavailable. Check notion token: zen-claw credentials set notion token <TOKEN>.",
)

DAILY_WORKFLOW = DeclarativeIntent(
    name="daily_workflow",
    description="Trigger daily assistant workflows (morning briefing, wrap-up, weekly report).",
    patterns=[
        # Chinese — morning briefing
        r"(?:做|来|生成|给我)(?:个|一个|份)?(?:早报|晨报|日报|今日摘要|每日摘要)",
        r"(?:今天|今日)(?:的)?(?:摘要|总结|概况|briefing)",
        # Chinese — wrap-up
        r"(?:做|来|生成|给我)(?:个|一个|份)?(?:晚报|日终总结|收工总结)",
        r"(?:今天|今日)(?:的)?(?:工作总结|日终报告|wrap)",
        # Chinese — weekly report
        r"(?:做|来|生成|给我)(?:个|一个|份)?(?:周报|每周总结|weekly)",
        r"(?:本周|这周)(?:的)?(?:总结|摘要|报告|summary)",
        # English
        r"\bmorning\s+briefing\b",
        r"\bdaily\s+(?:briefing|summary|digest|report)\b",
        r"\bend\s+of\s+day\b",
        r"\bwrap[\s-]?up\b",
        r"\bweekly\s+(?:report|summary|digest)\b",
    ],
    negative_patterns=[],
    params={},
    static_params={},
    tool_name="",  # No direct tool call — hybrid mode
    contract=_DAILY_WORKFLOW_CONTRACT,
    mode="hybrid",
    hybrid_skill="daily_assistant",
    failure_message="Daily assistant skill not available.",
)


# ── All daily intents for batch registration ───────────────────────────────────

DAILY_INTENTS: list[DeclarativeIntent] = [
    EMAIL_CHECK,
    CALENDAR_CHECK,
    REMINDER,
    NOTION_QUERY,
    DAILY_WORKFLOW,
]
