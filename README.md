# novel-lint

> 中文小说文风自检：机械可判定的交给脚本，需要判断力的交给大模型。

小说「写得不好」大部分时候不是玄学，是能被说清的技术问题：一句话里塞了五个信息点、
同一段里用了三次「像」、人物的情绪直接写成「他很难过」、两个角色说话的腔调一模一样、
矛盾没有来由、解决了也没有代价。

这些毛病里大约九成可以机械判定，剩下一成才真的需要理解力。大多数 AI 改文工具把两件事
混在一起做，结果是模型既不准，还会顺手把一句克制的白描改成浓妆。

novel-lint 把两层拆开：

| 脚本 | 依赖 | 干什么 |
|---|---|---|
| `writing_scan.py` | 无（纯标准库） | 正则 + 统计，毫秒级，零幻觉 |
| `writing_check.py` | 一个 LLM（本地或远程） | 只查需要判断力的那几条 |

两个脚本都输出 JSON，都能通过退出码报警，可以直接挂到 pre-commit 或 CI 上。

## 它查什么

七条规则，原作者自己定的写作铁律（全文在 [`rules/novel-rules.md`](rules/novel-rules.md)）：

| # | 规则 | 谁查 | 能抓出来的典型句子 |
|---|---|---|---|
| 1 | 叙事视角越界 | 模型 | 「他不知道，此刻他妹妹正在……」 |
| 2 | 排比堆砌 / 句式同质化 | 两边 | 「像潮水一样涨，像雾一样漫，像一场永远不会散的雨季」 |
| 3 | 角色扁平、功能化 | 模型 | 反派之所以是反派，只因为剧情需要一个反派 |
| 4 | 情绪直写 | 两边 | 「他很难过。」 |
| 5 | 元素重复 | 两边 | 一段里「味道」出现四次 |
| 6 | 信息过载 | 两边 | 一句话六个分号串起六个信息点 |
| 7 | 矛盾站不住 | 模型 | 冲突没有动机，解决没有代价 |

第 1、3、7 条必须模型；第 2、4、5、6 条脚本先粗筛，再交给模型复核。

## 安装

无第三方依赖，Python 3.9+。`writing_check.py` 也只用标准库（`urllib` 直接说话），
所以不需要 `pip install openai`。

    git clone https://github.com/y22t19053/novel-lint
    cd novel-lint

## 用法一：零依赖先扫一遍

    python writing_scan.py --file examples/chapter.txt

    [scan] 321 字，命中 6 处
      2. 排比堆砌: 2
      4. 情绪直写: 2
      5. 元素重复: 1
      6. 信息过载: 1
    报告: examples/chapter.scan.json

退出码默认是 0；加 `--fail-on-violations` 变成 1，方便当 hook 用：

    python writing_scan.py --file 章节.txt --fail-on-violations --quiet

也可以从管道读：`cat 章节.txt | python writing_scan.py --file -`

## 用法二：接模型，查需要判断力的规则

本地 Ollama（免费、不联网、适合平时写稿）：

    ollama serve
    ollama pull qwen3:8b
    python writing_check.py --file examples/chapter.txt

任意 OpenAI 兼容端点（OpenAI / DeepSeek / vLLM / Ollama 的 `/v1` 都行）：

    python writing_check.py --file 章节.txt \
        --backend openai --api-base https://api.deepseek.com/v1 \
        --api-key $env:OPENAI_API_KEY --model deepseek-chat

调试配置时常用的三个开关：

    python writing_check.py --file 章节.txt --dry-run     # 不调模型，只打印分段和规则
    python writing_check.py --file 章节.txt --only 1,3    # 只查视角和角色
    python writing_check.py --file 章节.txt --no-verify   # 跳过复核，快一倍，误报多

`--mode` 默认是 `auto`：两段以内的小文本一次把七条规则全问（更快、也更容易分清规则边界），
更长的章节自动切成逐条问（注意力不会被别的规则分走）。想固定行为就显式给 `--mode all`
或 `--mode per-rule`。

## 它输出什么

```json
{
  "rule": "4. 情绪直写",
  "quote": "他很难过。",
  "why": "直接命名情绪，没有给出情绪的外在表现或行为后果",
  "fix": "删掉这句，改用他此刻在做什么、手上动作出了什么差错来承载",
  "confidence": "high"
}
```

三个字段是刻意的：

- `quote` 必须能在原文里 grep 到 —— 报错不能是「我记得大概有」
- `why` 一句话说清违反了哪条规则 —— 说不清的就是误报
- `fix` 只允许删、换序、调详略，**不允许加形容词** —— 检查器的职责是让你看清问题，
  不是替你把这一段写了

## 复核（默认开着）

每条报出来的违规会被反过来问一遍：「如果作者是故意的，他为什么这么写？这个报错还站得住吗？」
站不住的丢掉，剩下的打上 `confidence`。加 `--keep-low` 连低置信的也保留。

实测它既砍得掉误报，也偶尔会砍掉真问题 —— 所以报告里每一条都该回原文核对一遍。
这正是 `quote` 必须逐字可 grep 的原因：你可以直接 `Ctrl+F` 验证，而不是信它。嫌碍事就
`--no-verify`。

## 换成你自己的规则

规则就是一份 markdown：编号 + 规则说明 + 正反例。用 `--rules my-rules.md` 换掉就行。

你的文风不必跟这份一样。它的价值不在「这七条是对的」，而在于展示：
**把你的审美判断拆成可执行的条文，机器就能替你守住下限，你把精力全花在上限上。**

## 局限（说在前面）

- 段落太短没意义，它是给「一整章」用的，不是给句子用的
- **本地小模型会「认出句子有问题、但归错类」**：把一句排比报成「氛围」，把情绪直写报成
  「视角越界」。同一句被多条规则同时报出时会自动合并成一条（`also_flagged_by` 记下是哪些
  规则）。所以：**信 `quote`，别信编号**。要更准就换更大的模型或走远程 API。
- 模型给的 `fix` 只是建议，不能直接采纳
- 第 7 条（矛盾）最不可靠，建议只当提示，别当结论
- 它不改写、不碰设定、不管剧情大纲 —— 那些不该交给工具

## 许可

MIT。规则文本随便改、随便商用。你要是加了条管用的，欢迎开个 issue 说说。
