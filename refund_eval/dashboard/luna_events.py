"""Fixed public vocabulary for the operator's read-only action timeline."""

MAX_STEPS = 10_000
MAX_FEED_BYTES = 4 * 1024 * 1024
STEP_FIELDS = {"seq", "at", "simulated_date", "turn", "action", "outcome"}
ACTIONS = {
    "inbox": "Check inbox", "message": "Read a message", "thread": "Read a conversation",
    "mail_search": "Search mail", "send": "Send a message", "reply": "Reply to a message",
    "search": "Search the web", "navigate": "Open a web page", "browser": "Use the browser",
    "schedule": "Schedule a wake-up", "turn": "Agent turn", "tool": "Use a tool",
}
OUTCOMES = {
    "returned": "Tool returned · payload hidden", "error": "Tool reported an error · details hidden",
    "cancelled": "Cancelled", "failed": "Tool failed · details hidden", "stopped": "Trial stopped",
    "recorded": "Recorded", "unknown": "Outcome not reported",
}
TOOL_ACTIONS = {
    "list_threads": "inbox", "get_message": "message", "get_thread": "thread",
    "search_threads": "mail_search", "send": "send", "reply": "reply",
    "web_search_exa": "search", "browser_navigate": "navigate", "schedule_wakeup": "schedule",
    **dict.fromkeys(("browser_click", "browser_type", "browser_snapshot", "browser_fill_form",
                     "browser_take_screenshot", "browser_wait_for", "browser_evaluate", "browser_run_code",
                     "browser_press_key", "browser_tabs", "browser_navigate_back"), "browser"),
}
