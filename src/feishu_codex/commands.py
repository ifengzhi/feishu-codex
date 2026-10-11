import argparse
import json
import shlex

from .config import UserError

HELP = """飞书私聊 / 本地命令（任务编号必须明确）：
create --workdir ~/ws/new-project --mkdir --key demo-1 [--thread 历史会话ID] -- 自然语言任务要求和背景
tasks [--all]（默认隐藏已结束任务；--all 包含历史）
status <任务编号>
continue <任务编号> <明确的后续要求>
append <任务编号> <追加要求>（运行中使用 turn/steer）
interrupt <任务编号>（停止当前 turn，允许后续继续）
cancel <任务编号>（取消此任务）
decide <任务编号> <请求编号> accept|decline|cancel（以请求实际允许选项为准）
answer <任务编号> <问题请求编号> {"问题ID":["回答"]}
diagnose
help
普通自然语言请放在 create 的 -- 后面或 append/continue 的任务编号之后。
飞书私聊中已明确选中任务时，也可直接发送自然语言；单个待回答问题优先接收答案，操作授权仍使用审批卡片。
不会根据“最后一个会话”猜测任务。终端控制期间飞书仅可查询。
"""


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UserError(message)


def parse(line):
    line = line.strip().removeprefix("/")
    op, _, tail = line.partition(" ")
    if op == "tasks":
        if tail.strip() not in ("", "--all"):
            raise UserError("用法：tasks [--all]")
        return {"op": op, "all": tail.strip() == "--all"}
    if op in ("help", "diagnose"):
        return {"op": op}
    if op == "create":
        header, separator, text = tail.partition(" -- ")
        if not separator:
            raise UserError("创建格式：create --workdir ~/ws/demo --mkdir --key demo-1 -- 任务正文")
        parser = Parser(add_help=False)
        parser.add_argument("--workdir", dest="cwd", required=True)
        parser.add_argument("--mkdir", action="store_true")
        parser.add_argument("--thread")
        parser.add_argument("--key")
        try:
            values = vars(parser.parse_args(shlex.split(header)))
        except ValueError as e:
            raise UserError(str(e)) from None
        return {"op": "create", "text": text, **values}
    if op in ("status", "cancel", "interrupt"):
        if len(tail.split()) != 1:
            raise UserError("必须提供一个明确任务编号")
        return {"op": op, "task": tail.strip()}
    if op in ("append", "continue"):
        tid, _, text = tail.partition(" ")
        if not tid or not text.strip():
            raise UserError("请指定任务编号和后续要求")
        return {"op": op, "task": tid, "text": text}
    if op in ("decide", "answer"):
        parts = tail.split(maxsplit=2)
        if len(parts) != 3:
            raise UserError("必须提供任务编号、请求编号和决定/回答")
        result = {"op": op, "task": parts[0], "request": parts[1]}
        try:
            result["decision" if op == "decide" else "answers"] = parts[2] if op == "decide" else json.loads(parts[2])
        except ValueError:
            raise UserError('回答格式为 JSON：{"问题ID":["回答"]}') from None
        return result
    raise UserError("不能确定目标任务。请使用 create 或 append <任务编号>；help 查看帮助。")


def render(result):
    if isinstance(result, dict) and "cwd" in result and "events" in result:
        t = result
        lines = [f"任务 {t['id']} | {t['status']} | 控制权 {t['control']}",
                 f"目录 {t['cwd']}", f"会话 {t['thread']} | turn {t['turn']}", t["summary"]]
        for r in t["requests"]:
            if r["status"] == "pending":
                lines.append(f"待处理 {r['id']} {r['method']}\n" + json.dumps(r["params"], ensure_ascii=False) +
                             f"\n允许决定：{r['decisions']}")
        failed = [n for n in t["notifications"] if n["status"] == "failed"]
        if failed:
            lines.append("通知发送失败：" + json.dumps(failed, ensure_ascii=False))
        return "\n".join(lines)
    return json.dumps(result, ensure_ascii=False, indent=2)
