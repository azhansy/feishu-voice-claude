#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""查看飞书->Claude 的任务:正在执行 + 历史。

用法:
  ./venv/bin/python tasks.py            # 看一次
  ./venv/bin/python tasks.py -n 30      # 历史看最近 30 条
  ./venv/bin/python tasks.py --watch    # 每 2 秒刷新(实时盯)
"""
import os
import sys
import json
import time

HERE = os.path.dirname(os.path.abspath(__file__))
RUNNING_FILE = os.path.join(HERE, "state", "running.json")
LEDGER_FILE = os.path.join(HERE, "tasks.jsonl")

ICON = {"done": "✔", "error": "✖", "timeout": "⏱"}


def retain_days():
    try:
        with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
            return json.load(f).get("dashboard", {}).get("retain_days", 2)
    except Exception:
        return 2


def ago(ts):
    d = max(0, int(time.time() - ts))
    if d < 60:
        return f"{d}秒前"
    if d < 3600:
        return f"{d // 60}分前"
    if d < 86400:
        return f"{d // 3600}小时前"
    return f"{d // 86400}天前"


def load_running():
    try:
        with open(RUNNING_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def load_recent(n):
    try:
        with open(LEDGER_FILE, encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return []
    cut = time.time() - retain_days() * 86400
    out = []
    for ln in lines:
        try:
            r = json.loads(ln)
            if r.get("ts", 0) >= cut:
                out.append(r)
        except Exception:
            pass
    return out[-n:]


def render(n):
    running = load_running()
    recent = load_recent(n)

    print("\n\033[1m● 正在执行\033[0m")
    if not running:
        print("  (空闲,没有正在跑的任务)")
    else:
        for r in running:
            secs = int(time.time() - r.get("start", time.time()))
            print(f"  #{r['id']}  [{r['project']}]  {r['instruction']}   ⏳ 已跑 {secs}s")

    print(f"\n\033[1m● 历史(最近 {len(recent)} 条)\033[0m")
    if not recent:
        print("  (还没有历史)")
    for r in recent:
        icon = ICON.get(r.get("status"), "•")
        head = (r.get("instruction") or "").replace("\n", " ")[:40]
        res = (r.get("result") or "").replace("\n", " ")[:60]
        print(f"  {icon} #{r['id']} {ago(r['ts']):>6}  [{r.get('project')}] "
              f"{head}  ({r.get('seconds')}s)")
        if res:
            print(f"      └ {res}")
    print()


def main():
    n = 15
    watch = "--watch" in sys.argv
    if "-n" in sys.argv:
        try:
            n = int(sys.argv[sys.argv.index("-n") + 1])
        except Exception:
            pass
    if watch:
        try:
            while True:
                os.system("clear")
                print(f"飞书→Claude 任务台账   {time.strftime('%H:%M:%S')}   (Ctrl+C 退出)")
                render(n)
                time.sleep(2)
        except KeyboardInterrupt:
            pass
    else:
        render(n)


if __name__ == "__main__":
    main()
