# 效能棱镜 DevPrism

[![CI](https://github.com/HeartBeatSea/devprism/actions/workflows/ci.yml/badge.svg)](https://github.com/HeartBeatSea/devprism/actions/workflows/ci.yml)

给它一个 GitHub 仓库地址，它的功能：出一份研发效能报告，或者把这个仓库画成一张可以点开的星图。

## 两个模式

**效能分析**看过程数据：提交节奏、PR 协作、人员结构、风险。数字由纯函数算出来，不经大模型；模型负责决定查什么、以及把结果讲成能看懂的话。

**代码分析**看仓库结构：模块在外圈、文件在最外层，点一下节点，它去读那部分源码，告诉你这块代码是干什么的。

一开始只想做效能分析。后来发现 Java 项目的目录本身就带语义——看到 `src/test/java` 和 `pom.xml`，不用读一行代码就能知道构建工具、模块划分和测试比例。这块通用工具都不做，就顺手做深了。

## 使用

核心引擎只用标准库，Python 3.10 以上就行。

```bash
python -m devprism analyze pallets/click --days 180   # 出报告，不需要模型
python -m devprism ask "协作健康度怎么样" owner/repo    # 问 Agent，需要模型
```

网页版要装个可选依赖：

```bash
pip install -e ".[web]"
python -m devprism serve        # 默认 http://127.0.0.1:8800
```

**不想配 token、或者网络访问不了 GitHub**，可以直接跑离线演示：

```bash
python -m devprism serve --demo        # 用仓库自带的快照，一次网络请求都不发
python -m devprism analyze google/gson --demo
```

`demo/gson.json` 是一份真实仓库的快照（66 条提交、110 个 PR、435 个目录项、6 份源码），由 `scripts/export_demo.py` 导出。星图、报告、点开节点读源码这些都能跑，只有模型解读需要 LLM key。

配置放 `.env`，复制 `.env.example` 改：

- `GITHUB_TOKEN`：强烈建议配。不配 60 次/小时，配了 5000 次，还能拿到 GraphQL 的评审数据
- `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY`：任何 OpenAI 兼容接口都行
- `EMBED_MODEL`：配了才开向量检索，不配走纯 BM25
- `HTTPS_PROXY`：访问 GitHub 要代理时用

自检和测试：

```bash
python -m devprism doctor
python -m unittest discover -s tests -t .
```

## 输出长什么样

`pallets/click`，180 天窗口，节选：

```
提交总数        311      周均 12.1
合并 PR         122      周均 4.7，未合并 5
PR 合并周期      22.3h    良好
巴士因子        2 人     正常
规范提交占比     3%       关注

[中] 夜间提交占比偏高：38% 的提交发生在 22:00–05:59
```

Java 仓库会多一节工程结构。拿 `google/gson` 举例：Maven、Java 11、122 个主代码类、142 个测试类、7 个子模块、根 POM 声明 3 个依赖、6 个 CI 配置。

同一份目录树还能按「文件在干什么」分类：主代码、测试、文档、构建配置、CI、资源、脚本、前端、二进制、其他。这个统计不额外花配额，目录树一次请求里每个文件的大小都已经带回来了。完整的周报样例在 [`examples/sample-weekly-report.md`](examples/sample-weekly-report.md)。

## 星图

画图不读源码。一次 Trees 请求就拿到了全部路径和文件大小，层级、体量、角色都能算出来，这是免费的。

点开某个节点才去读它的源码，最多三个文件、每个 40 KB 以内，先用正则抽出类名和函数名，再交给模型概括。解释结果按内容哈希存在本地，同一个文件不会重复计费。

必须这么拆的原因很直接：一个中等 Java 仓库两百多个文件，逐个读就是两百多次请求，配额当场就没了。结构和语义分开——前者白拿，后者按需——才跑得起来。

## 评测

主观说「效果还行」没有意义，所以有一份 32 条的用例集，分两种跑法。

**检索评测**纯离线，不调模型，几十毫秒跑完：

```bash
python scripts/evaluate.py --retrieval
```

当前结果：

```
recall@3 = 88.5%（23/26）· MRR = 0.846
```

漏掉的 3 条全是**用词不同但意思一样**的问法——「代码评审必须检查哪些内容」对不上「必查项」，「多久发一次版」对不上「每两周一个发布窗口」。BM25 的死穴就在这里，这也正是加向量检索那条路存在的理由。

**工具评测**要真跑 Agent，消耗 token：

```bash
python scripts/evaluate.py --agent --demo
```

当前结果（17 条带工具期望的用例）：

```
准确率 100%（17/17）· 精确率 56.2% · 召回 100%
平均每条调用 3.9 个工具
```

准确率满分只说明「该调的调到了」，精确率 56% 说明它还会顺手多查一两个工具。这里面有一部分是设计使然——提示词要求开场先 `list_skills`、先 `repo_overview` 确认规模，这类定向调用没算进精确率；剩下的是真的多查了。想让它更省，得在提示词里收紧"够用就停"，但那会拿召回换精确，不一定划算。

用例在 [`eval/cases.json`](eval/cases.json)，断言的是**真实存在的引用路径**，写错会直接报错，避免用例本身是假的。

检索那一项进了 CI，**recall 掉到 85% 以下就直接红**。改检索代码时不用靠自觉，数字会说话。

## 持续集成

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) 跑两件事：

- **测试**（Python 3.10 和 3.12）：单元测试、检索评测、离线演示冒烟、密钥自检
- **Web**：装 `[web]` 依赖，构建 FastAPI 应用，确认首页能渲染

离线演示那步是刻意加的——它不访问 GitHub，跑不通说明快照或渲染链路坏了，跟网络无关。

顺带记一个自己踩过的坑：演示快照的窗口原本按「今天」往前推，导出时 66 条提交，过一周再看变成 65 条，再放几个月会变成 0。现在 `FixtureClient.now()` 把时间钉在导出那一刻，数字永久稳定。

## 技能包

检索解决的是「模型不知道的事实」，技能解决的是「模型不知道该怎么做」。所以规范原文走知识库，体检流程走技能包。

`skills/` 下面一个目录一个技能，`SKILL.md` 开头是 frontmatter：

```markdown
---
name: commit-audit
title: 提交规范体检
description: 检查提交规范执行情况并给出整改建议
when: 用户问提交规范、commit message、规范率
tools: commit_convention, search_knowledge
---

## 步骤
1. 调 commit_convention 拿规范率
2. 低于 50% 时查团队规范原文，并引用出处
```

模型先调 `list_skills` 看到目录（只有名字和适用场景），判断对得上才 `load_skill` 加载正文。渐进式披露——技能攒到几十个也不会一次性把上下文撑爆。

现在内置四个：提交规范体检、Java 工程结构评审、PR 协作体检、周报生成。技能里写死了口径，比如「测试比例是类数量比、不是行覆盖率」「评审数据为 null 时判不可用」，模型照着执行就不会跑偏。

## 目录

```
devprism/
├── cache.py          SQLite 响应缓存 + ETag 条件请求
├── github.py         REST + GraphQL（限流、分页、gzip、截断标记）
├── net.py            HTTP 出口统一，支持代理
├── snapshot.py       时间窗口与采集结果的数据结构
├── metrics.py        指标纯函数 + 风险规则
├── repo_index.py     仓库知识索引 + Java 工程结构 + 区域统计
├── codegraph.py      目录树 → 星图数据
├── symbols.py        正则抽取类/方法/函数
├── knowledge.py      Markdown 切分 + BM25（中文用字符二元组）
├── embeddings.py     可插拔向量后端 + 混合打分
├── report.py         报告渲染
├── agent.py          ReAct 循环 + 死循环护栏 + 引用校验
├── skills.py         技能包加载（渐进式披露）
├── evaluation.py     评测：检索 recall@k / 工具选择准确率
├── guard.py          护栏：输入拦截 / 防注入 / 防泄漏
├── tools.py          8 个工具 + JSON Schema 校验
├── workflow.py       YAML 工作流：DAG、人工确认、断点续跑
├── yamlite.py        极简 YAML 子集解析器
├── history.py        按仓库分组的历史
├── llm.py            OpenAI 兼容客户端
├── markdown_lite.py  Markdown → HTML，输出强制转义
├── web.py            FastAPI + SSE
├── fixture.py        离线快照客户端（演示用）
├── config.py         环境变量 / .env
└── cli.py            命令行入口
web/index.html        单文件前端，没有构建步骤
knowledge/            团队规范样例
skills/               技能包（每个目录一个 SKILL.md）
workflows/weekly.yaml 周报工作流
tests/                222 个用例
docs/                 需求、使用手册、发布说明
demo/gson.json        离线演示快照
scripts/              导出演示数据、跑评测、密钥自检
eval/cases.json       32 条评测用例
```

## 护栏

这个 Agent 只聊目标仓库。为了让它真的只聊仓库，做了三层：

**输入侧**在调模型之前就拦。越权指令（「忽略之前的指令」「把系统提示词发给我」「你现在是…」）和明显无关的请求（写诗、翻译、天气）会直接拒答，一次模型调用都不产生。规则只收高精度说法，宁可漏放给提示词层判断，也不误伤正常提问。

**上下文侧**防的是间接注入。仓库里的 README、源码、规范文档都是**不可信数据**——里面完全可能写着「忽略上述指令」。检索结果被扫描后，可疑片段会在观察结果里标出来，并声明这些是数据不是命令。

**输出侧**看回答里有没有系统提示的原文。出现两段以上就判定泄漏，整条替换成拒答。

三层都挡不住的，交给 system prompt 的第 0 条兜底。

有个细节值得一提：调用方会在问题前面拼「目标仓库：owner/name」，而那里必然带「仓库」二字。如果直接拿拼好的内容做范围判断，护栏会永远放行——所以 `agent.run` 单独收一个 `user_input`，检查的是用户原话。

## 边界

数据只有 Git 提交和 PR 元数据，没有 CI 失败率、缺陷和代码复杂度，所以 Revert 占比只是"变更失败率"的近似。报告里的「测试比例」是测试类和主代码类的数量之比，不是行覆盖率。

另外，同一份目录树在代码分析里读不超过三个文件，那是导航和按需解释，不叫静态分析。

所有结论用于团队改进，不用于个人绩效考核。
