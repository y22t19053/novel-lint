#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""writing_scan.py — 确定性规则扫描层。

不依赖任何大模型：零幻觉、毫秒级、结果可复现。
只负责规则里「机械可判定」的那一半；需要判断力的（视角越界、角色扁平、
矛盾站不站得住）交给 writing_check.py。

查这几类：
  2. 排比堆砌    —— 60 字内三个以上「像」的比喻连排
  4. 情绪直写    —— 「很难过 / 很开心」这类情绪词直接写出来
  5. 元素重复    —— 同一个双字词在 200 字窗口里出现 5 次以上
  6. 信息过载    —— 单句用「；」串起四个以上并列信息点

用法:
  python writing_scan.py --file 章节.txt
  python writing_scan.py --file 章节.txt --out report.json
  python writing_scan.py --file 章节.txt --fail-on-violations   # 有命中则 exit 1，可进 CI
  type 章节.txt | python writing_scan.py --file -
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

__version__ = "0.1.0"

# Windows 重定向到文件/管道时默认用 GBK，中文会变乱码
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 规则 4：情绪直写。命中前 4 字以内有否定词的跳过（「不难过」不算直写）。
EMOTION = (
    "很难过", "很开心", "很害怕", "很愤怒", "很痛苦", "很悲伤", "很恐惧", "很焦虑", "很绝望",
    "难过", "开心", "害怕", "恐惧", "愤怒", "悲伤", "痛苦", "焦虑", "绝望", "兴奋", "紧张",
)
NEG = ("不", "没", "没有", "不是", "不会", "别", "几乎不", "说不上")

# 高频虚词 / 通用词：出现再多也不是「元素重复」，否则整篇都是噪音。
STOP = {
    "了一", "色的", "有人", "没有", "一个", "什么", "自己", "这个", "那个", "可以", "已经",
    "时候", "地方", "东西", "出来", "起来", "不是", "就是", "还是", "但是", "因为", "所以",
    "然后", "现在", "他们", "我们", "你们", "她们", "他的", "她的", "我的", "你的", "看着",
    "他说", "她说", "说道", "一下", "一样", "一直", "一起", "上面", "下面", "里面", "外面",
    "左边", "右边", "前面", "后面", "开始", "到了", "的那", "的是", "上的", "里的",
}


def split_sents(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[。！？!?…])", text) if s.strip()]


def scan_emotion(text: str) -> list[dict]:
    hits = []
    for m in re.finditer("|".join(EMOTION), text):
        pre = text[max(0, m.start() - 4):m.start()]
        if any(pre.endswith(n) for n in NEG):
            continue
        hits.append({
            "rule": "4. 情绪直写",
            "word": m.group(0),
            "context": text[max(0, m.start() - 20):m.end() + 20],
        })
    return hits


def scan_simile_run(text: str) -> list[dict]:
    """60 字窗口内出现三个以上「像」，视为比喻连排。"""
    hits = []
    for m in re.finditer("像", text):
        seg = text[m.start():m.start() + 60]
        if seg.count("像") >= 3:
            hits.append({
                "rule": "2. 排比堆砌",
                "context": text[max(0, m.start() - 10):m.start() + 60],
            })
    seen, uniq = set(), []
    for h in hits:
        key = h["context"][:20]
        if key not in seen:
            seen.add(key)
            uniq.append(h)
    return uniq


def scan_repeat(text: str, win: int = 200, n: int = 5, word_len: int = 2) -> list[dict]:
    """同一个双字词在 200 字窗口出现 >= n 次，视为元素/句式反复。"""
    hits, seen = [], set()
    for i in range(0, max(1, len(text) - win), 20):
        seg = text[i:i + win]
        cnt: dict[str, int] = {}
        for j in range(len(seg) - word_len + 1):
            w = seg[j:j + word_len]
            if not re.match(r"[\u4e00-\u9fa5]{2}", w):
                continue
            cnt[w] = cnt.get(w, 0) + 1
        for w, c in cnt.items():
            if c >= n and w not in seen and w not in STOP:
                seen.add(w)
                hits.append({
                    "rule": "5. 元素重复",
                    "word": w,
                    "count": c,
                    "context": text[max(0, i - 20):i + 80],
                })
    return hits[:8]


def scan_density(text: str) -> list[dict]:
    """单句用「；」串起四个以上并列信息点：信息点一次给得太密。

    只认分号句，不认逗号——逗号密集的正常长句太多，宁可漏报也不误报。
    """
    hits = []
    for s in split_sents(text):
        semi = s.count("；") + s.count(";")
        if semi >= 3:
            hits.append({
                "rule": "6. 信息过载",
                "semicolons": semi,
                "context": s[:80],
            })
    return hits


def scan_exclaim(text: str) -> list[dict]:
    n = text.count("！") + text.count("!")
    if n >= 2 and n * 500 > len(text):
        return [{"rule": "4. 情绪堆叠", "count": n,
                 "context": "全文 %d 字出现 %d 个感叹号" % (len(text), n)}]
    return []


def scan(text: str) -> list[dict]:
    return (scan_emotion(text) + scan_simile_run(text)
            + scan_repeat(text) + scan_density(text) + scan_exclaim(text))


def read_text(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    text = Path(path).read_text(encoding="utf-8-sig")
    return re.sub(r"[ \t]+", "", text)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="中文小说确定性文风扫描（零依赖、零幻觉）")
    ap.add_argument("--file", required=True, help="待检文本；用 - 表示从 stdin 读")
    ap.add_argument("--out", default="", help="JSON 报告路径，默认 <文件>.scan.json")
    ap.add_argument("--quiet", action="store_true", help="只写 JSON，不打印摘要")
    ap.add_argument("--fail-on-violations", action="store_true", help="有命中时退出码为 1")
    ap.add_argument("--version", action="version", version="writing_scan " + __version__)
    args = ap.parse_args(argv)

    try:
        text = read_text(args.file)
    except FileNotFoundError:
        print("[error] 找不到文件: %s" % args.file, file=sys.stderr)
        return 2
    if not text.strip():
        print("[error] 文本为空", file=sys.stderr)
        return 2

    hits = scan(text)
    by_rule: dict[str, list[dict]] = {}
    for h in hits:
        by_rule.setdefault(h["rule"], []).append(h)

    if not args.quiet:
        print("[scan] %d 字，命中 %d 处" % (len(text), len(hits)))
        for rule, items in sorted(by_rule.items()):
            print("  %s: %d" % (rule, len(items)))
            for it in items[:6]:
                label = it.get("word") or it.get("count") or ""
                print("    - %-6s | %s" % (label, it["context"].replace("\n", " ")[:60]))

    if args.out:
        out = args.out
    elif args.file == "-":
        out = "stdin.scan.json"
    else:
        out = re.sub(r"\.txt$", "", args.file) + ".scan.json"

    Path(out).write_text(
        json.dumps({"file": args.file, "chars": len(text), "hits": hits},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if not args.quiet:
        print("报告: %s" % out)

    if args.fail_on_violations and hits:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
