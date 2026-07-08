#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞书语音 -> 本机 Claude Code 桥接程序

链路:
  飞书机器人(长连接) -> 收到语音/文字消息
    -> 下载语音 -> ffmpeg 转 wav -> faster-whisper 本地识别(免费/离线)
    -> 解析项目名 + 指令 -> 危险操作确认 -> 调用本机 claude -p 执行
    -> 结果回发到飞书

全程 0 费用:识别在本机做,飞书只用免费的收发消息 + 长连接事件。
"""

import os
import re
import sys
import json
import time
import uuid
import shutil
import tempfile
import difflib
import threading
import warnings
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 屏蔽 Xcode 自带 Python(LibreSSL)与 urllib3 的兼容告警,避免刷屏 bridge.err.log
warnings.filterwarnings("ignore")

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    GetMessageResourceRequest,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
    P2ImMessageReceiveV1,
)
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

HERE = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def load_config():
    path = os.path.join(HERE, "config.json")
    if not os.path.exists(path):
        sys.exit("缺少 config.json,请先 cp config.example.json config.json 并填写。")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


CFG = load_config()

# 待确认任务: { chat_id: {"project": str, "instruction": str, "ts": float, "token": str} }
PENDING = {}
PENDING_TTL = 300  # 秒
# PENDING 与 LAST_PROJECT 都会被多个消息线程读写,统一用这把锁保护「检查后再改」
_PENDING_LOCK = threading.Lock()

# 事件幂等:飞书事件为「至少一次」投递,断连重投/用户手抖会重复触发。
# 记录近期已处理的事件标识(消息用 message_id,卡片确认用一次性 token),避免重复执行。
_SEEN = {}                 # id -> ts
_SEEN_LOCK = threading.Lock()
SEEN_TTL = 600             # 秒


def seen_before(eid):
    """首次见到返回 False 并登记;重复出现返回 True。顺带清理过期项。"""
    if not eid:
        return False
    now = time.time()
    with _SEEN_LOCK:
        for k in [k for k, v in _SEEN.items() if now - v > SEEN_TTL]:
            _SEEN.pop(k, None)
        if eid in _SEEN:
            return True
        _SEEN[eid] = now
        return False

# 会话记忆: {"<chat_id>::<project>": "<claude session uuid>"},持久化到 state/sessions.json
SESSIONS = {}
SESSIONS_FILE = os.path.join(HERE, "state", "sessions.json")
_SESSIONS_LOCK = threading.Lock()

# 每个会话最近用过的项目,用于「不重复报项目名」的追问
LAST_PROJECT = {}


def set_pending(chat_id, project, instruction, token):
    with _PENDING_LOCK:
        PENDING[chat_id] = {"project": project, "instruction": instruction,
                            "ts": time.time(), "token": token}


def get_pending(chat_id):
    """取待确认任务;顺便按 TTL 过期清理。"""
    with _PENDING_LOCK:
        p = PENDING.get(chat_id)
        if p and time.time() - p["ts"] >= PENDING_TTL:
            PENDING.pop(chat_id, None)
            return None
        return p


def pop_pending(chat_id, token=None):
    """弹出待确认任务;若给了 token 但对不上(重复点击/过期卡片),返回 None 且不弹出。"""
    with _PENDING_LOCK:
        p = PENDING.get(chat_id)
        if p is None:
            return None
        if token is not None and p.get("token") != token:
            return None
        return PENDING.pop(chat_id, None)


def set_last_project(chat_id, project):
    with _PENDING_LOCK:
        LAST_PROJECT[chat_id] = project


def get_last_project(chat_id):
    with _PENDING_LOCK:
        return LAST_PROJECT.get(chat_id)

# 项目使用次数(持久化,不受台账 2 天清理影响),用于选择卡片排序
USAGE = {}
USAGE_FILE = os.path.join(HERE, "state", "usage.json")
_USAGE_LOCK = threading.Lock()


def _load_usage():
    global USAGE
    try:
        with open(USAGE_FILE, encoding="utf-8") as f:
            USAGE = json.load(f)
    except Exception:
        USAGE = {}


def bump_usage(project):
    with _USAGE_LOCK:
        USAGE[project] = USAGE.get(project, 0) + 1
        try:
            os.makedirs(os.path.dirname(USAGE_FILE), exist_ok=True)
            with open(USAGE_FILE, "w", encoding="utf-8") as f:
                json.dump(USAGE, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


def projects_ranked():
    """全部项目按使用次数降序;次数相同按名字。"""
    return sorted(list_projects(), key=lambda p: (-USAGE.get(p, 0), p))

# 重置上下文的口令(整句匹配)
RESET_WORDS = ["重置上下文", "清空上下文", "清空记忆", "新会话", "新话题", "忘掉之前", "reset", "new chat"]


def _session_key(chat_id, project):
    return f"{chat_id}::{project}"


def _load_sessions():
    global SESSIONS
    try:
        with open(SESSIONS_FILE, encoding="utf-8") as f:
            SESSIONS = json.load(f)
    except Exception:
        SESSIONS = {}


def _save_sessions():
    try:
        os.makedirs(os.path.dirname(SESSIONS_FILE), exist_ok=True)
        with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(SESSIONS, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

# whisper 模型延迟加载(首次用到才加载,避免启动慢)
_WHISPER = None
_WHISPER_LOCK = threading.Lock()

# 全局飞书 API client(发消息/下载资源用)
API = lark.Client.builder() \
    .app_id(CFG["feishu"]["app_id"]) \
    .app_secret(CFG["feishu"]["app_secret"]) \
    .build()


# --------------------------------------------------------------------------- #
# 任务台账:正在执行的任务 + 历史记录(供 tasks.py 查看)
# --------------------------------------------------------------------------- #
STATE_DIR = os.path.join(HERE, "state")
os.makedirs(STATE_DIR, exist_ok=True)
LEDGER_FILE = os.path.join(HERE, "tasks.jsonl")       # 历史(每行一条)
RUNNING_FILE = os.path.join(STATE_DIR, "running.json")  # 正在执行的快照

_LEDGER_LOCK = threading.Lock()
_RUNNING = {}   # tid -> {id, project, instruction, start}
_TASK_SEQ = 0

RETAIN_DAYS = CFG.get("dashboard", {}).get("retain_days", 2)


def _cutoff_ts():
    return time.time() - RETAIN_DAYS * 86400


def _prune_ledger():
    """只保留最近 RETAIN_DAYS 天的历史(重写 tasks.jsonl)。"""
    try:
        if not os.path.exists(LEDGER_FILE):
            return
        cut = _cutoff_ts()
        with open(LEDGER_FILE, encoding="utf-8") as f:
            lines = f.readlines()
        kept = []
        for ln in lines:
            try:
                if json.loads(ln).get("ts", 0) >= cut:
                    kept.append(ln)
            except Exception:
                pass
        with open(LEDGER_FILE, "w", encoding="utf-8") as f:
            f.writelines(kept)
    except Exception:
        pass


def _max_ledger_id():
    """扫描历史台账里最大的任务 id,重启后据此续号,避免 id 跨重启从 1 重复。"""
    mx = 0
    try:
        with open(LEDGER_FILE, encoding="utf-8") as f:
            for ln in f:
                try:
                    mx = max(mx, int(json.loads(ln).get("id", 0)))
                except Exception:
                    pass
    except Exception:
        pass
    return mx


def ledger_startup():
    """启动时:清理过期历史 + 复位残留的『正在执行』(上次进程已退出)+ 续号任务 id。"""
    global _TASK_SEQ
    _prune_ledger()
    with _LEDGER_LOCK:
        _TASK_SEQ = _max_ledger_id()
        _RUNNING.clear()
        _write_running()


def _write_running():
    try:
        with open(RUNNING_FILE, "w", encoding="utf-8") as f:
            json.dump(list(_RUNNING.values()), f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def task_start(project, instruction):
    global _TASK_SEQ
    with _LEDGER_LOCK:
        _TASK_SEQ += 1
        tid = _TASK_SEQ
        _RUNNING[tid] = {"id": tid, "project": project,
                         "instruction": instruction, "start": time.time()}
        _write_running()
    print(f"[task#{tid}] ▶ 开始  [{project}] {instruction}", flush=True)
    return tid


def task_end(tid, status, seconds, result):
    with _LEDGER_LOCK:
        info = _RUNNING.pop(tid, {})
        _write_running()
        rec = {
            "id": tid, "ts": time.time(),
            "project": info.get("project"), "instruction": info.get("instruction"),
            "status": status, "seconds": round(seconds, 1),
            "result": (result or "").strip()[:20000],   # 存完整结果供弹窗查看
        }
        try:
            with open(LEDGER_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass
    _prune_ledger()
    icon = {"done": "✔", "error": "✖", "timeout": "⏱"}.get(status, "•")
    print(f"[task#{tid}] {icon} {status}  {round(seconds, 1)}s  [{info.get('project')}]", flush=True)


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def get_whisper():
    global _WHISPER
    if _WHISPER is None:
        with _WHISPER_LOCK:
            if _WHISPER is None:
                from faster_whisper import WhisperModel
                model_name = CFG["whisper"]["model"]
                print(f"[whisper] 加载模型 {model_name} ...", flush=True)
                # int8 在 CPU 上又快又省内存
                _WHISPER = WhisperModel(model_name, device="cpu", compute_type="int8")
                print("[whisper] 模型就绪", flush=True)
    return _WHISPER


def reply(message_id, text):
    """回复到原消息所在会话(thread)。"""
    body = ReplyMessageRequestBody.builder() \
        .content(json.dumps({"text": text}, ensure_ascii=False)) \
        .msg_type("text") \
        .build()
    req = ReplyMessageRequest.builder() \
        .message_id(message_id) \
        .request_body(body) \
        .build()
    resp = API.im.v1.message.reply(req)
    if not resp.success():
        print(f"[reply] 失败 code={resp.code} msg={resp.msg}", flush=True)


def reply_card(message_id, card):
    """回复一张交互卡片(带按钮)。"""
    body = ReplyMessageRequestBody.builder() \
        .content(json.dumps(card, ensure_ascii=False)) \
        .msg_type("interactive") \
        .build()
    req = ReplyMessageRequest.builder() \
        .message_id(message_id) \
        .request_body(body) \
        .build()
    resp = API.im.v1.message.reply(req)
    if not resp.success():
        print(f"[reply_card] 失败 code={resp.code} msg={resp.msg}", flush=True)


def confirm_card(project, instruction, danger, token):
    """确认任务的交互卡片:两个按钮 确认执行 / 取消。token 用于防重复点击。"""
    title = "⚠️ 请确认(疑似危险操作)" if danger else "📋 请确认要执行的任务"
    template = "red" if danger else "blue"
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": template,
                   "title": {"tag": "plain_text", "content": title}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md",
                "content": f"**项目**:{project}\n**任务**:{instruction}"}},
            {"tag": "action", "actions": [
                {"tag": "button",
                 "text": {"tag": "plain_text", "content": "✅ 确认执行"},
                 "type": "primary",
                 "value": {"act": "confirm", "project": project,
                           "instruction": instruction, "token": token}},
                {"tag": "button",
                 "text": {"tag": "plain_text", "content": "✖️ 取消"},
                 "type": "default",
                 "value": {"act": "cancel"}},
            ]},
            {"tag": "note", "elements": [
                {"tag": "plain_text", "content": "也可直接回复「确认」/「取消」"}]},
        ],
    }


def project_picker_card(instruction):
    """没识别到项目时的选择卡片:按使用次数排序,常用的做快捷按钮,全部放下拉。
    选中项目后会再弹一张确认卡片(二次确认),不会直接执行。"""
    ranked = projects_ranked()          # 使用次数降序,只用项目名
    quick = ranked[:6]

    elements = [{"tag": "div", "text": {"tag": "lark_md",
                 "content": f"**任务**:{instruction}\n请选择项目(选中后会再确认一次,不会直接执行):"}}]

    # 快捷按钮(每行 3 个),只显示项目名
    row = []
    for p in quick:
        row.append({"tag": "button",
                    "text": {"tag": "plain_text", "content": p},
                    "type": "default",
                    "value": {"act": "pick", "project": p, "instruction": instruction}})
        if len(row) == 3:
            elements.append({"tag": "action", "actions": row})
            row = []
    if row:
        elements.append({"tag": "action", "actions": row})

    # 全部项目下拉(同样按使用次数排序,只显示项目名)
    options = [{"text": {"tag": "plain_text", "content": p}, "value": p} for p in ranked]
    elements.append({"tag": "action", "actions": [
        {"tag": "select_static",
         "placeholder": {"tag": "plain_text", "content": "全部项目…"},
         "options": options,
         "value": {"act": "pick", "instruction": instruction}}]})

    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "orange",
                   "title": {"tag": "plain_text", "content": "🤔 选择项目"}},
        "elements": elements,
    }


def download_audio(message_id, file_key, out_dir):
    """下载飞书语音文件,返回本地路径。"""
    req = GetMessageResourceRequest.builder() \
        .message_id(message_id) \
        .file_key(file_key) \
        .type("file") \
        .build()
    resp = API.im.v1.message_resource.get(req)
    if not resp.success():
        raise RuntimeError(f"下载语音失败 code={resp.code} msg={resp.msg}")
    raw = os.path.join(out_dir, "voice.opus")
    with open(raw, "wb") as f:
        f.write(resp.file.read())
    return raw


def to_wav(src, out_dir):
    """ffmpeg 转 16k 单声道 wav,给 whisper。"""
    wav = os.path.join(out_dir, "voice.wav")
    subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-ar", "16000", "-ac", "1", wav],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return wav


def _whisper_hotwords():
    """把中文别名 + 常用项目名喂给 whisper 作热词,提升「特斯卡」「哨兵」等专有名词的识别率。
    initial_prompt 实际只保留末尾约 224 token,故优先中文口令,项目名按使用次数取前若干个。"""
    aliases = list(CFG.get("aliases", {}).keys())
    try:
        ranked = projects_ranked()[:20]
    except Exception:
        ranked = []
    names = [n for n in dict.fromkeys(aliases + ranked) if n]  # 去重保序,别名在前
    return ("常用项目名:" + "、".join(names)) if names else None


def transcribe(wav_path):
    model = get_whisper()
    segments, _ = model.transcribe(
        wav_path,
        language=CFG["whisper"].get("language") or None,
        initial_prompt=_whisper_hotwords(),
        vad_filter=True,
    )
    return "".join(s.text for s in segments).strip()


# --------------------------------------------------------------------------- #
# 项目名解析
# --------------------------------------------------------------------------- #
def list_projects():
    root = CFG["projects_root"]
    return [
        d for d in os.listdir(root)
        if os.path.isdir(os.path.join(root, d)) and not d.startswith(".")
    ]


def match_project(text):
    """
    从口令里找项目名。返回 (project_dir_name, 去掉项目名后的指令) 或 (None, text)。
    优先级: 别名表 > 目录名子串 > 模糊匹配。
    """
    projects = list_projects()
    aliases = CFG.get("aliases", {})
    norm = text.lower()

    # 1) 别名(中文口令)
    for alias, target in aliases.items():
        if alias.lower() in norm:
            rest = re.sub(re.escape(alias), "", text, count=1, flags=re.IGNORECASE)
            return target, rest.strip(" ,，。:：")

    # 2) 目录名直接出现在文本里
    for p in sorted(projects, key=len, reverse=True):
        if p.lower() in norm:
            rest = re.sub(re.escape(p), "", text, count=1, flags=re.IGNORECASE)
            return p, rest.strip(" ,，。:：")

    # 3) 模糊匹配第一个词
    first = re.split(r"[\s,，。:：]+", text.strip(), maxsplit=1)
    head = first[0] if first else ""
    near = difflib.get_close_matches(head.lower(), [p.lower() for p in projects], n=1, cutoff=0.6)
    if near:
        matched = next(p for p in projects if p.lower() == near[0])
        rest = first[1] if len(first) > 1 else ""
        return matched, rest.strip(" ,，。:：")

    # 4) 兜底默认项目
    default = CFG.get("default_project")
    if default:
        return default, text.strip()

    return None, text


# --------------------------------------------------------------------------- #
# 危险检测 + 执行
# --------------------------------------------------------------------------- #
def is_dangerous(instruction):
    if not CFG["danger"].get("require_confirm", True):
        return False
    low = instruction.lower()
    compact = low.replace(" ", "")          # 中文关键词按去空格匹配:「把库清空」也能命中「清空」
    for kw in CFG["danger"]["keywords"]:
        k = kw.lower().strip()
        if not k:
            continue
        if re.search(r"[a-z0-9]", k):
            # 英文/命令类:词边界匹配,'rm' 命中「rm foo」但不误伤「arm」;无需再配 'rm ' 带空格版
            if re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", low):
                return True
        elif k in compact:
            return True
    return False


def _norm(text):
    return text.strip().lower().rstrip("。.!!~,, ")


def is_confirm(text):
    # 只认「整条消息就是确认词」,避免新指令里恰好含确认词被误触发
    return _norm(text) in [w.lower() for w in CFG["danger"]["confirm_words"]]


def is_cancel(text):
    return _norm(text) in [w.lower() for w in CFG["danger"].get("cancel_words", [])]


def is_reset(text):
    return _norm(text) in [w.lower() for w in RESET_WORDS]


def reset_session(chat_id, project):
    """清空某个『会话+项目』的上下文,下次指令将开新会话。返回是否有清除。"""
    key = _session_key(chat_id, project)
    with _SESSIONS_LOCK:
        existed = SESSIONS.pop(key, None) is not None
        _save_sessions()
    return existed


def run_claude(project, instruction, chat_id=None):
    """在指定项目目录调用本机 claude -p 执行,返回文字结果。

    按「会话+项目」维度续接 Claude 上下文:首次用 --session-id 建会话,
    之后用 --resume 续接,让连续对话能记住前文。
    """
    cwd = os.path.join(CFG["projects_root"], project)
    if not os.path.isdir(cwd):
        return f"项目目录不存在: {cwd}"

    env = os.environ.copy()
    proxy = CFG["claude"].get("proxy")
    if proxy:
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                  "http_proxy", "https_proxy", "all_proxy"):
            env[k] = proxy
        np = CFG["claude"].get("no_proxy", "localhost,127.0.0.1,::1")
        env["NO_PROXY"] = np
        env["no_proxy"] = np

    base = [CFG["claude"]["bin"], "-p", instruction, "--output-format", "json"]
    extra = CFG["claude"].get("extra_args", [])
    timeout = CFG["claude"].get("timeout_seconds", 1800)

    key = _session_key(chat_id, project)
    with _SESSIONS_LOCK:
        sid = SESSIONS.get(key)
    resuming = bool(sid)
    if not sid:
        sid = str(uuid.uuid4())

    def _invoke(session_flag):
        return subprocess.run(base + session_flag + extra, cwd=cwd, env=env,
                              capture_output=True, text=True, timeout=timeout)

    bump_usage(project)
    tid = task_start(project, instruction)
    t0 = time.time()
    try:
        if resuming:
            proc = _invoke(["--resume", sid])
            # 续接失败(会话可能已失效)-> 用新会话重来一次
            if proc.returncode != 0 and not proc.stdout.strip():
                sid = str(uuid.uuid4())
                resuming = False
                proc = _invoke(["--session-id", sid])
        else:
            proc = _invoke(["--session-id", sid])
    except subprocess.TimeoutExpired:
        out = "⏱️ Claude 执行超时了。"
        task_end(tid, "timeout", time.time() - t0, out)
        return out

    if proc.returncode != 0 and not proc.stdout.strip():
        out = f"❌ Claude 执行失败:\n{proc.stderr.strip()[:1500]}"
        task_end(tid, "error", time.time() - t0, out)
        return out

    # 解析 --output-format json 的结果,并记住会话 id
    try:
        data = json.loads(proc.stdout)
        result = data.get("result") or data.get("text") or proc.stdout
        sid = data.get("session_id") or sid
    except (json.JSONDecodeError, AttributeError):
        result = proc.stdout.strip()
    with _SESSIONS_LOCK:
        SESSIONS[key] = sid
        _save_sessions()

    full = result.strip()
    out = full[:3500] or "(Claude 没有返回文本)"
    if len(full) > 3500:
        out += "\n\n…(结果较长已截断,完整内容见台账 http://127.0.0.1:8765 或 bridge.log)"
    task_end(tid, "done", time.time() - t0, full or out)   # 台账存完整,Feishu 回截断版
    return out


def run_and_reply(message_id, project, instruction, chat_id):
    """确认后统一入口:回执「执行中」→ 跑 claude(其间定时心跳)→ 回结果。
    长任务(claude -p 可能跑几分钟到 30 分钟)期间飞书侧不再是死等。"""
    reply(message_id, f"✅ 已确认,在 [{project}] 执行中…\n> {instruction}")
    interval = CFG["claude"].get("heartbeat_seconds", 60)
    stop = threading.Event()

    def _beat():
        t0 = time.time()
        while interval and not stop.wait(interval):
            reply(message_id, f"⏳ 还在跑…已 {int(time.time() - t0)}s")

    hb = threading.Thread(target=_beat, daemon=True)
    hb.start()
    try:
        out = run_claude(project, instruction, chat_id)
    finally:
        stop.set()
    reply(message_id, out)


# --------------------------------------------------------------------------- #
# 消息处理
# --------------------------------------------------------------------------- #
def handle_text(message_id, chat_id, text):
    text = text.strip()
    if not text:
        return

    # 1) 若已有待确认任务,先判断这条是确认 / 取消 / 还是新指令
    pending = get_pending(chat_id)
    if pending:
        if is_confirm(text):
            popped = pop_pending(chat_id)
            if popped:                       # 可能已被卡片按钮抢先消费,pop 到才执行
                set_last_project(chat_id, popped["project"])
                run_and_reply(message_id, popped["project"], popped["instruction"], chat_id)
            return
        if is_cancel(text):
            pop_pending(chat_id)
            reply(message_id, "已取消,未执行任何操作。")
            return
        # 其它内容:当作新指令,覆盖旧的待确认任务(继续往下走)

    # 2) 不带项目名的重置口令(如「重置上下文」)-> 重置本会话上次用的项目
    if is_reset(text):
        last = get_last_project(chat_id)
        if not last:
            reply(message_id, "本会话还没用过任何项目,无需重置。下条指令带上项目名即可开新会话。")
            return
        reset_session(chat_id, last)
        reply(message_id, f"🧹 已为项目 [{last}] 开启新会话,之后的对话不再带旧上下文。")
        return

    # 3) 解析新指令 -> 一律先复述、等二次确认,绝不直接执行
    project, instruction = match_project(text)
    if not project:
        # 没带项目名:若本会话之前用过某项目,则续用它(整句作为指令)
        last = get_last_project(chat_id)
        if last:
            project, instruction = last, text
        else:
            # 没上下文可续:发选择卡片,点按钮/下拉选项目
            reply_card(message_id, project_picker_card(text))
            return

    # 3.5) 带项目名的重置(如「<项目名> 新会话」)-> 只重置该项目
    if is_reset(instruction):
        reset_session(chat_id, project)
        set_last_project(chat_id, project)
        reply(message_id, f"🧹 已为项目 [{project}] 开启新会话,之后的对话不再带旧上下文。")
        return

    if not instruction:
        reply(message_id, f"识别到项目 [{project}],但没听到要做什么。")
        return

    set_last_project(chat_id, project)
    token = str(uuid.uuid4())
    set_pending(chat_id, project, instruction, token)
    reply_card(message_id, confirm_card(project, instruction, is_dangerous(instruction), token))


def handle_audio(message_id, chat_id, content):
    file_key = content.get("file_key")
    if not file_key:
        return
    tmp = tempfile.mkdtemp(prefix="fvc_")
    try:
        raw = download_audio(message_id, file_key, tmp)
        wav = to_wav(raw, tmp)
        text = transcribe(wav)
        if not text:
            reply(message_id, "没听清,能再说一遍吗?")
            return
        reply(message_id, f"🎙️ 识别到:{text}")
        handle_text(message_id, chat_id, text)
    except Exception as e:
        reply(message_id, f"处理语音出错:{e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def on_message(data: P2ImMessageReceiveV1) -> None:
    try:
        msg = data.event.message
        message_id = msg.message_id
        chat_id = msg.chat_id
        mtype = msg.message_type

        # 幂等:同一条消息被飞书重投时 message_id 不变,已处理过就跳过,避免重复执行
        if seen_before(message_id):
            print(f"[on_message] 跳过重复投递 message_id={message_id}", flush=True)
            return

        content = json.loads(msg.content)

        if mtype == "audio":
            # whisper 可能较慢,丢到线程里,避免阻塞事件循环
            threading.Thread(
                target=handle_audio, args=(message_id, chat_id, content), daemon=True
            ).start()
        elif mtype == "text":
            threading.Thread(
                target=handle_text, args=(message_id, chat_id, content.get("text", "")), daemon=True
            ).start()
        # 其它类型忽略
    except Exception as e:
        print(f"[on_message] 异常: {e}", flush=True)


def on_card(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
    """卡片按钮点击回调:确认执行 / 取消。"""
    try:
        value = (data.event.action.value or {}) if data.event.action else {}
        ctx = data.event.context
        chat_id = ctx.open_chat_id if ctx else None
        msg_id = ctx.open_message_id if ctx else None
        act = value.get("act")

        if act == "confirm":
            project = value.get("project")
            instruction = value.get("instruction")
            token = value.get("token")
            # 幂等:凭 token 一次性消费待确认任务;重复点击/打字已确认过 -> pop 不到,直接忽略
            if chat_id and pop_pending(chat_id, token) is None:
                return P2CardActionTriggerResponse(
                    {"toast": {"type": "info", "content": "该任务已处理过了"}})
            if chat_id:
                set_last_project(chat_id, project)

            def _run():
                if msg_id:
                    run_and_reply(msg_id, project, instruction, chat_id)
                else:
                    run_claude(project, instruction, chat_id)

            threading.Thread(target=_run, daemon=True).start()
            return P2CardActionTriggerResponse({"toast": {"type": "info", "content": "开始执行…"}})

        if act == "pick":
            # 从选择卡片选了项目:按钮带 project;下拉在 action.option 里
            project = value.get("project")
            if not project and data.event.action:
                project = data.event.action.option
            instruction = value.get("instruction")
            if not project:
                return P2CardActionTriggerResponse({"toast": {"type": "error", "content": "未选择项目"}})
            token = str(uuid.uuid4())
            if chat_id:
                set_last_project(chat_id, project)
                set_pending(chat_id, project, instruction, token)
            if msg_id:
                reply_card(msg_id, confirm_card(project, instruction, is_dangerous(instruction), token))
            return P2CardActionTriggerResponse({"toast": {"type": "info", "content": f"已选 {project}"}})

        # 取消
        if chat_id:
            pop_pending(chat_id)
        if msg_id:
            reply(msg_id, "已取消,未执行任何操作。")
        return P2CardActionTriggerResponse({"toast": {"type": "info", "content": "已取消"}})
    except Exception as e:
        print(f"[on_card] 异常: {e}", flush=True)
        return P2CardActionTriggerResponse({"toast": {"type": "error", "content": "处理失败"}})


# --------------------------------------------------------------------------- #
# 本地网页台账(浏览器访问 http://127.0.0.1:<port>)
# --------------------------------------------------------------------------- #
DASHBOARD_HTML = r"""<!doctype html>
<html lang="zh"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>飞书 → Claude 任务台账</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1a1a1a;--sub:#6b7280;--line:#e5e7eb;--accent:#2563eb;--run:#f59e0b;--ok:#16a34a;--err:#dc2626}
@media(prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#181b21;--fg:#e6e7e9;--sub:#9aa0aa;--line:#282c34;--accent:#3b82f6}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft Yahei",sans-serif}
.wrap{max-width:900px;margin:0 auto;padding:24px 16px 60px}
h1{font-size:20px;margin:0 0 4px}.meta{color:var(--sub);font-size:13px;margin-bottom:20px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--ok);margin-right:6px;vertical-align:middle}
h2{font-size:14px;color:var(--sub);text-transform:uppercase;letter-spacing:.05em;margin:26px 0 10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:10px}
.row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.badge{background:var(--accent);color:#fff;font-size:12px;padding:2px 9px;border-radius:99px;font-weight:600}
.instr{font-weight:600;flex:1;min-width:180px;word-break:break-all}
.time{color:var(--sub);font-size:13px;white-space:nowrap}
.st{font-size:13px;font-weight:600}.st.done{color:var(--ok)}.st.error,.st.timeout{color:var(--err)}
.run .badge{background:var(--run)}
.spin{width:14px;height:14px;border:2px solid var(--run);border-top-color:transparent;border-radius:50%;display:inline-block;animation:s .8s linear infinite}
@keyframes s{to{transform:rotate(360deg)}}
.preview{margin-top:6px;color:var(--sub);font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.card.click{cursor:pointer}.card.click:hover{border-color:var(--accent)}
.more{color:var(--accent);font-size:12px;margin-left:6px}
.empty{color:var(--sub);font-size:14px;padding:8px 2px}
/* 弹窗 */
.mask{position:fixed;inset:0;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;padding:20px;z-index:9}
.mask[hidden]{display:none}
.modal{background:var(--card);border:1px solid var(--line);border-radius:14px;max-width:760px;width:100%;max-height:85vh;display:flex;flex-direction:column;box-shadow:0 12px 40px rgba(0,0,0,.3)}
.mhead{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:16px 18px;border-bottom:1px solid var(--line)}
.mhead .x{margin-left:auto;cursor:pointer;color:var(--sub);font-size:22px;line-height:1;border:none;background:none}
.msub{padding:10px 18px 0;color:var(--sub);font-size:13px}
.mtitle{padding:6px 18px 0;font-weight:600;word-break:break-word}
.mbody{margin:12px 18px 18px;padding:12px 14px;background:var(--bg);border:1px solid var(--line);border-radius:10px;white-space:pre-wrap;word-break:break-word;overflow:auto;font-size:13px;line-height:1.6;flex:1}
</style></head><body><div class="wrap">
<h1><span class="dot"></span>飞书 → Claude 任务台账</h1>
<div class="meta" id="meta">连接中…</div>
<h2>正在执行</h2><div id="running"></div>
<h2>历史</h2><div id="recent"></div>
</div>
<div class="mask" id="mask" hidden><div class="modal">
 <div class="mhead" id="mHead"></div>
 <div class="mtitle" id="mInstr"></div>
 <div class="msub" id="mMeta"></div>
 <div class="mbody" id="mBody"></div>
</div></div>
<script>
const ICON={done:"✔",error:"✖",timeout:"⏱"};
let lastSig=null, serverNow=0, baseMono=0, TASKS={};
function ago(s){s=Math.max(0,s|0);if(s<60)return s+"秒前";if(s<3600)return(s/60|0)+"分前";if(s<86400)return(s/3600|0)+"小时前";return(s/86400|0)+"天前"}
function esc(t){return(t||"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]))}
function nowTs(){return serverNow+(performance.now()/1000-baseMono)}
function sig(d){return d.running.map(r=>r.id).join(",")+"|"+d.recent.map(r=>r.id+r.status).join(",")}
function renderLists(d){
 const now=d.now;
 const R=document.getElementById("running");
 R.innerHTML=d.running.length?d.running.map(r=>`<div class="card run"><div class="row"><span class="spin"></span><span class="badge">${esc(r.project)}</span><span class="instr">${esc(r.instruction)}</span><span class="time">已跑 <span class="elapsed" data-start="${r.start}">${(now-r.start)|0}</span>s</span></div></div>`).join(""):`<div class="empty">空闲,没有正在跑的任务</div>`;
 const H=document.getElementById("recent");
 H.innerHTML=d.recent.length?d.recent.map(r=>`<div class="card click" data-id="${r.id}"><div class="row"><span class="st ${r.status}">${ICON[r.status]||"•"} ${r.status}</span><span class="badge">${esc(r.project)}</span><span class="instr">${esc(r.instruction)}</span><span class="time">${ago(now-r.ts)} · ${r.seconds}s<span class="more">查看›</span></span></div>${r.result?`<div class="preview">${esc(r.result)}</div>`:""}</div>`).join(""):`<div class="empty">还没有历史</div>`;
}
function tickElapsed(){
 const now=nowTs();
 document.querySelectorAll(".elapsed").forEach(el=>{el.textContent=Math.max(0,(now-parseFloat(el.dataset.start))|0)});
}
function openModal(id){
 const r=TASKS[id]; if(!r)return;
 document.getElementById("mHead").innerHTML=`<span class="st ${r.status}">${ICON[r.status]||"•"} ${r.status}</span><span class="badge">${esc(r.project)}</span><button class="x" onclick="closeModal()">×</button>`;
 document.getElementById("mInstr").textContent=r.instruction||"";
 document.getElementById("mMeta").textContent=`#${r.id} · ${new Date(r.ts*1000).toLocaleString()} · 耗时 ${r.seconds}s`;
 document.getElementById("mBody").textContent=r.result||"(无输出)";
 document.getElementById("mask").hidden=false;
}
function closeModal(){document.getElementById("mask").hidden=true}
document.getElementById("recent").addEventListener("click",e=>{const c=e.target.closest(".card.click");if(c&&c.dataset.id)openModal(c.dataset.id)});
document.getElementById("mask").addEventListener("click",e=>{if(e.target.id==="mask")closeModal()});
document.addEventListener("keydown",e=>{if(e.key==="Escape")closeModal()});
async function pull(){
 let d;try{d=await(await fetch("/api/tasks",{cache:"no-store"})).json()}catch(e){document.getElementById("meta").textContent="⚠️ 未连接到 bridge";return}
 serverNow=d.now; baseMono=performance.now()/1000;
 TASKS={}; d.recent.forEach(r=>TASKS[r.id]=r);
 document.getElementById("meta").textContent=`共 ${d.recent.length} 条历史 · 正在执行 ${d.running.length} 个 · ${new Date().toLocaleTimeString()}`;
 const s=sig(d);
 if(s!==lastSig){          // 仅当任务集合变化才重绘,并保留滚动位置
  lastSig=s;
  const y=window.scrollY;
  renderLists(d);
  window.scrollTo(0,y);
 }
}
pull();setInterval(pull,2000);setInterval(tickElapsed,1000);
</script></body></html>"""


def _api_tasks():
    try:
        with open(RUNNING_FILE, encoding="utf-8") as f:
            running = json.load(f)
    except Exception:
        running = []
    recent = []
    try:
        with open(LEDGER_FILE, encoding="utf-8") as f:
            lines = f.readlines()
        n = CFG.get("dashboard", {}).get("recent", 50)
        cut = _cutoff_ts()
        for ln in lines:
            try:
                r = json.loads(ln)
                if r.get("ts", 0) >= cut:      # 只保留最近 N 天
                    recent.append(r)
            except Exception:
                pass
        recent = recent[-n:]
        recent.reverse()  # 最新在前
    except Exception:
        pass
    return {"running": running, "recent": recent, "now": time.time()}


class _DashHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # 静音访问日志

    def do_GET(self):
        if self.path.startswith("/api/tasks"):
            body = json.dumps(_api_tasks(), ensure_ascii=False).encode("utf-8")
            ctype = "application/json; charset=utf-8"
        elif self.path == "/" or self.path.startswith("/?"):
            body = DASHBOARD_HTML.encode("utf-8")
            ctype = "text/html; charset=utf-8"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_dashboard():
    dcfg = CFG.get("dashboard", {})
    if not dcfg.get("enabled", True):
        return
    port = dcfg.get("port", 8765)
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), _DashHandler)
    except OSError as e:
        print(f"[dashboard] 启动失败(端口 {port}?): {e}", flush=True)
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[dashboard] 台账页面 -> http://127.0.0.1:{port}", flush=True)


# --------------------------------------------------------------------------- #
# 启动
# --------------------------------------------------------------------------- #
def main():
    handler = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(on_message) \
        .register_p2_card_action_trigger(on_card) \
        .build()

    client = lark.ws.Client(
        CFG["feishu"]["app_id"],
        CFG["feishu"]["app_secret"],
        event_handler=handler,
        log_level=lark.LogLevel.INFO,
    )
    _load_sessions()
    _load_usage()
    ledger_startup()
    start_dashboard()
    print("[bridge] 长连接启动,等待飞书消息… (Ctrl+C 退出)", flush=True)
    client.start()


if __name__ == "__main__":
    main()
