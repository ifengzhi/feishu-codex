"""Feishu Card JSON 2.0 presentation; task content remains Markdown."""
import json
import re

STATES = {"queued": "排队中", "starting": "正在启动", "running": "执行中", "waiting": "等待处理",
          "completed": "已完成", "failed": "失败", "interrupted": "已中断", "cancelled": "已结束",
          "cancelling": "正在结束", "pending": "待处理", "resolved": "已处理", "invalidated": "已失效"}


def plain(text):
    return {"tag": "div", "text": {"tag": "plain_text", "content": str(text)}}


def code(text, language="plain_text"):
    text = str(text)
    fence = "`" * max(3, 1 + max((len(x) for x in re.findall(r"`+", text)), default=0))
    return f"{fence}{language}\n{text}\n{fence}"


def markdown(text, limit=16000):
    """Bound JSON bytes, close truncated fences, and disable generated HTML mentions.

    Preserve code verbatim. Split tables into separate components to avoid Feishu's
    limit of four Markdown tables per component.
    """
    parts, lines, size, fence, truncated = [], [], 0, None, False
    for line in str(text).splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        next_fence = fence
        if marker:
            token, suffix = marker.groups()
            if fence is None:
                next_fence = token
            elif token[0] == fence[0] and len(token) >= len(fence) and not suffix.strip():
                next_fence = None
        safe = line if fence or marker else line.replace("<", "&lt;")
        cost = len(json.dumps(safe, ensure_ascii=False).encode()) + 2
        if size + cost > limit or len(parts) >= 60:
            truncated = True
            # A single long line must still produce a useful preview.
            remaining = limit - size - 16 if len(parts) < 60 else 0
            while safe and len(json.dumps(safe, ensure_ascii=False).encode()) > remaining:
                safe = safe[:max(0, len(safe) - max(1, len(safe) // 8))]
            if safe and not marker:
                lines.append(safe + "…")
            break
        # A separator starts a new table; keep its header with it.
        if fence is None and lines and "|" in line and re.fullmatch(r"[\s|:\-]+", line) and "---" in line:
            header = lines.pop()
            if lines:
                parts.append("\n".join(lines))
            lines = [header]
        lines.append(safe)
        size += cost
        fence = next_fence
    if fence:
        lines.append(fence)
    if lines:
        parts.append("\n".join(lines))
    elements = [{"tag": "markdown", "content": p} for p in parts if p.strip()]
    if truncated:
        elements.append(plain("内容较长，卡片仅显示前段；完整结果请用 feishu-codex status <任务编号> 查看。"))
    return elements or [plain("暂无内容")]


def card(title, elements, color="blue"):
    # Upgrade legacy layout helpers and queued UI cards without changing callback values.
    converted = []
    for element in elements:
        if element["tag"] == "action":
            buttons = []
            for old in element["actions"]:
                b = dict(old)
                b["behaviors"] = [{"type": "callback", "value": b.pop("value")}]
                buttons.append({"tag": "column", "width": "auto", "elements": [b]})
            converted.append({"tag": "column_set", "flex_mode": "flow", "columns": buttons})
        else:
            converted.append(element)
    return {"schema": "2.0", "config": {"update_multi": True, "width_mode": "fill"},
            "header": {"title": {"tag": "plain_text", "content": title}, "template": color},
            "body": {"elements": converted}}


def metadata(task):
    return plain(f"任务：{task['id']} · {STATES.get(task['status'], task['status'])}\n目录：{task['cwd']}")


def content_text(body):
    if isinstance(body, str):
        return body
    if isinstance(body, dict):
        if body.get("type") == "agentMessage":
            return body.get("text") or "正在处理任务…"
        if body.get("type") == "commandExecution":
            return "**执行命令**\n\n" + code(body.get("command", ""), "shell") + (
                f"\n\n退出码：{body['exitCode']}" if body.get("exitCode") is not None else "")
        if "summary" in body and "id" in body:
            return body["summary"] or "暂无结果"
        if body.get("type") == "fileChange":
            return "**文件变更**\n\n" + code(json.dumps(body.get("changes", []), ensure_ascii=False, indent=2), "json")
    return code(json.dumps(body, ensure_ascii=False, indent=2), "json")


def notification_card(row, body, task=None):
    titles = {"completed": "任务完成", "progress": "任务进展", "failed": "任务失败", "error": "任务异常",
              "interrupted": "任务中断", "cancelled": "任务已结束", "control": "控制权变更", "reply": "Codex 回复"}
    color = {"completed": "green", "failed": "red", "error": "red", "interrupted": "orange"}.get(row["kind"], "blue")
    elements = [metadata(task), {"tag": "hr"}] if task else []
    elements += markdown(content_text(body))
    return card(titles.get(row["kind"], "任务通知"), elements, color)
