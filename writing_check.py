#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""writing_check.py — 需要判断力的规则，交给大模型。

按 rules/novel-rules.md 里的规则逐条检查一段中文小说，输出结构化 JSON：
每条违规都带原文引用（quote）、违反理由（why）、具体改法（fix）。
默认还会做一次「二次复核」：把第一遍报出的每条违规再问一遍模型，站不住的丢掉。

后端两种，都只依赖标准库：
  --backend ollama  本地 Ollama（默认 http://127.0.0.1:11434）
  --backend openai  任意 OpenAI 兼容端点（OpenAI / DeepSeek / vLLM / Ollama 的 /v1）

用法:
  python writing_check.py --file 章节.txt
  python writing_check.py --file 章节.txt --only 1,3        # 只查视角和角色
  python writing_check.py --file 章节.txt --no-verify       # 跳过二次复核，快但误报多
  python writing_check.py --file 章节.txt --backend openai \
      --api-base https://api.deepseek.com/v1 --api-key $env:OPENAI_API_KEY --model deepseek-chat
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

__version__ = "0.1.0"

# Windows 重定向到文件/管道时默认用 GBK，中文会变乱码
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

DEFAULT_RULES = Path(__file__).resolve().parent / "rules" / "novel-rules.md"
DEFAULT_API = {"ollama": "http://127.0.0.1:11434", "openai": "https://api.openai.com/v1"}
DEFAULT_MODEL = {"ollama": "qwen3:8b", "openai": ""}

SYS_ALL = """你是中文小说编辑，只做规则检查，不改写全文。用户会给你《写作铁律》和一段小说正文。
逐条检查正文是否违反铁律，只报有证据的违规。输出严格 JSON（不要 markdown 代码块）：
{"violations":[{"rule":"铁律编号+名称","quote":"正文中不超过40字的原文片段","why":"为什么违反，一句话","fix":"具体怎么改，一句话"}],"overall":"一句话总评"}
没有违规就返回 {"violations":[],"overall":"..."}。禁止编造原文中不存在的句子。"""

SYS_ONE = """你是中文小说编辑。这次只检查一条规则：第 {no} 条「{name}」。
规则原文：{rule}
本次重点（照这个找，不要泛泛而谈）：{hint}

【精确性要求，比召回更重要】
1. 克制的白描、留白、短句不是违规。作者写得越省，你越不该让他加修辞。
2. 你给出的 fix 不允许新增比喻、形容词、心理描写，也不允许把句子改得更"文学"——那正是这些规则要禁止的。
3. 只有当原文里确实存在这条规则的典型病灶时才报：能指出具体的词或结构。
4. 每段最多报 3 条最确定的。拿不准就不报。

用户给你一段小说正文。只报有原文证据的违规。输出严格 JSON（不要 markdown 代码块）：
{{"violations":[{{"quote":"正文中不超过40字的原文","why":"为什么违反这条规则，一句话","fix":"不新增修辞的具体改法","confidence":"high|medium"}}],"overall":"这条规则的一句话结论"}}
没有违规返回 {{"violations":[],"overall":"无"}}。禁止编造原文中不存在的句子。"""

SYS_VERIFY = """你是严格的中文小说编辑，在做二次复核。第一遍检查报了一处违规，你要判断它是否成立。
判定标准：
- 原文里必须真的能指认出这条规则的病灶（不是「可以写得更好」，而是「确实违规」）。
- 改法如果新增了比喻、形容词、心理描写，一律算不合格（这些规则本身就禁止加修辞）。
只输出 JSON：{"keep": true 或 false, "reason": "一句话"}"""

# 每条规则最容易漏掉的具体形态。逐条检查比只给规则原文召回高得多。
RULE_HINTS = {
    1: "角色知道了自己不可能知道的事：别人的内心想法、没打开过的抽屉/保险柜里的东西、尚未发生的未来；或者叙述直接钻进非视角人物的脑子。",
    2: "排比与堆砌（三个以上「像…像…像…」）、同一句式反复、形容词失衡、多个人物说话腔调一模一样。",
    3: "贴标签式人物（「她是坏人」「他很温柔」）、只为剧情功能存在的人（「因为剧情需要一个凶手」）、角色替作者解释设定。",
    4: "只查两种：①直接写情绪词（很难过/很开心/愤怒/害怕）而没有落地到身体反应或行为；②情绪跳变没有铺垫。只是动作、只是沉默、只是短句，都不算违规。",
    5: "只查两种：①同一个意象/比喻在一段里重复出现两次以上；②比喻与场景无逻辑关联（沉默像爆米花一样蔓延）。单个贴切比喻不算违规。",
    6: "一段话里塞进三个以上新信息点、信息密度忽高忽低、该展开的地方一句话带过。",
    7: "矛盾没有来源（「她毫无理由地恨他」）、矛盾解决得太轻易、冲突双方有一方站不住。",
}


# ---------------------------------------------------------------- 后端


def post_json(url: str, payload: dict, api_key: str | None = None, timeout: int = 600) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:400]
        raise RuntimeError("HTTP %s: %s" % (e.code, body))
    except urllib.error.URLError as e:
        raise RuntimeError(
            "连不上 %s（%s）。Ollama 需要先 `ollama serve`，默认端口 11434。" % (url, e.reason))


def ask(cfg, system: str, user: str, num_ctx: int = 16384) -> dict:
    """问一次模型，返回解析后的 JSON。"""
    if cfg.backend == "openai":
        url = cfg.api_base.rstrip("/") + "/chat/completions"
        payload = {
            "model": cfg.model,
            "temperature": 0.2,
            "stream": False,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }
        data = post_json(url, payload, cfg.api_key, cfg.timeout)
        content = data["choices"][0]["message"]["content"]
    else:
        url = cfg.api_base.rstrip("/") + "/api/chat"
        payload = {
            "model": cfg.model,
            "stream": False,
            "think": False,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "options": {"temperature": 0.2, "num_ctx": num_ctx},
        }
        data = post_json(url, payload, cfg.api_key, cfg.timeout)
        content = data["message"]["content"]
    return extract_json(content)


def extract_json(s: str) -> dict:
    """模型偶尔会在 JSON 外面套 markdown 或加几句话，这里尽量捞回来。"""
    s = (s or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.M).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    start = s.find("{")
    if start < 0:
        raise ValueError("模型没有返回 JSON：%s" % s[:200])
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(s[start:i + 1])
    raise ValueError("JSON 不完整：%s" % s[:200])


# ---------------------------------------------------------------- 规则


def load_rules(path: Path) -> dict[int, tuple[str, str]]:
    """把 '## N. 名称' 拆成 {N: (名称, 正文)}；拆不出来时整体当一条。"""
    if not path.exists():
        raise SystemExit("[error] 规则文件不存在: %s\n       用 --rules 指定，或从 rules/novel-rules.md 复制一份。" % path)
    txt = path.read_text(encoding="utf-8-sig")
    rules: dict[int, tuple[str, str]] = {}
    parts = re.split(r"^##\s*([0-9]+)\.\s*(.+)$", txt, flags=re.M)
    for i in range(1, len(parts) - 2, 3):
        try:
            no = int(parts[i])
        except ValueError:
            continue
        name = parts[i + 1].strip()
        body = parts[i + 2].split("\n## ")[0].strip()
        rules[no] = (name, body)
    return rules or {1: ("全部规则", txt[:4000])}


def chunks(text: str, size: int) -> list[str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    buf, out = "", []
    for p in paras:
        if len(buf) + len(p) + 2 > size and buf:
            out.append(buf)
            buf = p
        else:
            buf = ("%s\n\n%s" % (buf, p)) if buf else p
    if buf:
        out.append(buf)
    return out


def verify_one(cfg, no: int, name: str, rule: str, v: dict) -> tuple[bool, str]:
    msg = ("规则第 %d 条「%s」原文：%s\n\n被指原文：%s\n理由：%s\n改法：%s\n\n这处是否真的违反该规则？"
           % (no, name, rule[:1200], v.get("quote", ""), v.get("why", ""), v.get("fix", "")))
    try:
        data = ask(cfg, SYS_VERIFY, msg, num_ctx=8192)
        return bool(data.get("keep")), (data.get("reason") or "")[:80]
    except Exception as e:  # 复核失败不推翻第一遍结果
        return True, "verify 失败:%s" % e


# ---------------------------------------------------------------- 主流程


# ---------------------------------------------------------------- 去重
def dedupe(vs):
    """同一句话常被几条规则同时报出来。只留证据最强的第一条，其余记进 also_flagged_by。

    小模型尤其爱干这事：它认出这句有问题，但归错类。留着三条只会让人以为错了三处。
    """
    rank = {"high": 3, "medium": 2, "low": 1}
    kept, seen = [], {}
    for v in vs:
        key = re.sub(r"\s+", "", str(v.get("quote", "")))[:50]
        if key and key in seen:
            first = seen[key]
            if rank.get(v.get("confidence"), 2) > rank.get(first.get("confidence"), 2):
                first["confidence"] = v.get("confidence")
                if v.get("fix"):
                    first["fix"] = v["fix"]
            first.setdefault("also_flagged_by", []).append(v.get("rule"))
            continue
        if key:
            seen[key] = v
        kept.append(v)
    return kept


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="中文小说规则检查（可判定规则 + 大模型判断）")
    ap.add_argument("--file", required=True, help="待检文本；用 - 表示从 stdin 读")
    ap.add_argument("--rules", default=str(DEFAULT_RULES), help="规则文件（默认 rules/novel-rules.md）")
    ap.add_argument("--backend", choices=["ollama", "openai"], default="ollama")
    ap.add_argument("--model", default="", help="默认：ollama 用 qwen3:8b；openai 必填")
    ap.add_argument("--api-base", default="", help="默认 ollama=http://127.0.0.1:11434，openai=https://api.openai.com/v1")
    ap.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY", ""), help="默认读 OPENAI_API_KEY")
    ap.add_argument("--timeout", type=int, default=600, help="单次请求超时秒数")
    ap.add_argument("--chunk", type=int, default=1200, help="每段字数")
    ap.add_argument("--mode", choices=["auto", "per-rule", "all"], default="auto",
                    help="auto 短文一次全问、长章逐条问（默认）；per-rule 逐条问，召回最高；"
                         "all 一次全问，快但长文容易漏检")
    ap.add_argument("--only", default="", help="只查这几条，如 1,3")
    ap.add_argument("--keep-low", action="store_true", help="保留 confidence=low 的低置信条目")
    ap.add_argument("--no-verify", dest="verify", action="store_false", help="跳过二次复核（更快，但误报多）")
    ap.add_argument("--out", default="", help="JSON 报告路径，默认 <文件>.check.json")
    ap.add_argument("--dry-run", action="store_true", help="只打印分段与规则，不调用模型")
    ap.add_argument("--fail-on-violations", action="store_true", help="有违规时退出码为 1")
    ap.add_argument("--version", action="version", version="writing_check " + __version__)
    args = ap.parse_args(argv)

    cfg = argparse.Namespace(
        backend=args.backend,
        model=args.model or DEFAULT_MODEL[args.backend],
        api_base=args.api_base or DEFAULT_API[args.backend],
        api_key=args.api_key,
        timeout=args.timeout,
    )
    if not cfg.model:
        print("[error] --backend openai 需要显式指定 --model，例如 deepseek-chat / gpt-4o-mini", file=sys.stderr)
        return 2

    try:
        text = (sys.stdin.read() if args.file == "-"
                else Path(args.file).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        print("[error] 找不到文件: %s" % args.file, file=sys.stderr)
        return 2

    rules = load_rules(Path(args.rules))
    todo = ([int(x) for x in args.only.split(",") if x.strip().isdigit()]
            if args.only else sorted(rules))
    parts = chunks(text, args.chunk)
    out_path = args.out or (os.path.splitext(args.file)[0] if args.file != "-" else "stdin") + ".check.json"
    # 短文本一次全问更准也更快；长章逐条问，注意力不会被其它规则分走
    mode = args.mode
    if mode == "auto":
        mode = "all" if len(parts) <= 2 else "per-rule"

    print("[check] %d 字 / %d 段 / 规则 %s / backend=%s / model=%s / mode=%s"
          % (len(text), len(parts), todo, cfg.backend, cfg.model, mode))
    if args.dry_run:
        for i, p in enumerate(parts, 1):
            print("  段 %d: %d 字  %s..." % (i, len(p), p[:40].replace("\n", " ")))
        print("  模式: %s（%s）" % (mode, "短文一次全问" if mode == "all" else "长文逐条问"))
        return 0

    report = {"file": args.file, "model": cfg.model, "backend": cfg.backend,
              "mode": mode, "rules": todo, "violations": [], "chunks": []}
    t0 = time.time()

    if mode == "all":
        rules_txt = "\n\n".join("## %s. %s\n%s" % (n, rules[n][0], rules[n][1]) for n in todo)
        for i, part in enumerate(parts, 1):
            msg = "《写作铁律》：\n%s\n\n—— 待检正文（第%d/%d段）——\n%s" % (rules_txt, i, len(parts), part)
            try:
                data = ask(cfg, SYS_ALL, msg)
            except Exception as e:
                print("[warn] 第%d段失败: %s" % (i, e))
                continue
            vios = data.get("violations", []) or []
            if not args.keep_low:
                vios = [v for v in vios if (v.get("confidence") or "high") != "low"]
            for v in vios:
                v["chunk"] = i
                # 模型返回的 rule 是自由文本，取开头的编号去比对规则表
                m = re.match(r"\s*(\d+)", str(v.get("rule", "")))
                no = int(m.group(1)) if m and int(m.group(1)) in rules else None
                if no is not None:
                    name, body = rules[no]
                    if args.verify:
                        ok, reason = verify_one(cfg, no, name, body, v)
                        v["verify_reason"] = reason
                        if not ok:
                            continue
                    v.update({"rule_no": no, "rule": "%d. %s" % (no, name)})
                report["violations"].append(v)
            report["chunks"].append({"i": i, "overall": data.get("overall", ""), "n": len(vios)})
            print("[%d/%d] 违规 %d 条" % (i, len(parts), len(vios)))
    else:
        for no in todo:
            if no not in rules:
                print("[warn] 规则文件里没有第 %d 条，跳过" % no)
                continue
            name, body = rules[no]
            sysmsg = SYS_ONE.format(no=no, name=name, rule=body,
                                    hint=RULE_HINTS.get(no, "按规则原文逐句检查。"))
            found = 0
            for i, part in enumerate(parts, 1):
                try:
                    data = ask(cfg, sysmsg, "—— 待检正文（第%d/%d段）——\n%s" % (i, len(parts), part))
                except Exception as e:
                    print("[warn] 规则%d 第%d段失败: %s" % (no, i, e))
                    continue
                vios = data.get("violations", []) or []
                if not args.keep_low:
                    vios = [v for v in vios if (v.get("confidence") or "high") != "low"]
                vios = vios[:3]  # 每段每规则封顶 3 条，压住模型凑数
                for v in vios:
                    if args.verify:
                        ok, reason = verify_one(cfg, no, name, body, v)
                        v["verify_reason"] = reason
                        if not ok:
                            continue
                    v.update({"rule_no": no, "rule": "%d. %s" % (no, name), "chunk": i})
                    report["violations"].append(v)
                    found += 1
            print("[规则 %d. %s] %d 条" % (no, name, found))
            report["chunks"].append({"rule": no, "n": found})

    report["violations"] = dedupe(report["violations"])
    report["elapsed_s"] = round(time.time() - t0, 1)
    Path(out_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("[done] 共 %d 条违规，%ss，报告: %s"
          % (len(report["violations"]), report["elapsed_s"], out_path))
    for v in report["violations"]:
        print("  - [%s] %s  ->  %s" % (v.get("rule"), v.get("quote", "")[:30], v.get("fix", "")[:60]))

    if args.fail_on_violations and report["violations"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
