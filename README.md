# 效能棱镜 DevPrism

> 基于 RAG 与 AI Agent 的 GitHub 研发效能分析系统 · 专攻 Java 仓库

输入一个 GitHub 仓库，它做三件事：

1. **把仓库当成知识库** —— 目录树 + README / `pom.xml` / CI 配置进索引，Agent 能回答"这个项目怎么构建、依赖怎么管"；
2. **Agent 实时问答** —— 自己决定查哪些工具，结论带数据溯源；
3. **产出研发效能报告** —— 交付节奏、协作健康、Java 工程结构、风险提示，Markdown 可直接归档。

选 Java 是因为它的结构规整：`src/main/java`、`src/test/java`、`pom.xml` 这些路径本身就带语义，不读源码也能判断构建工具、模块划分和测试比例。通用仓库分析工具不会做这一层。

**当前状态**：指标引擎、仓库知识库、Java 结构分析、Agent 循环、YAML 工作流、分析历史均已可用，117 个单元测试。

---

## 打开网页版

```bash
pip install -e ".[web]"     # 仅 Web 控制台需要，核心引擎不用装任何东西
python -m devprism serve    # 默认 http://127.0.0.1:8800
```

左侧选仓库、中间对话、右侧实时出指标卡与分布图。结论里的每个数字都能追溯到工具返回。

配置写在 `.env`：

| 变量 | 作用 | 建议 |
| --- | --- | --- |
| `GITHUB_TOKEN` | 配额 60 → 5000 次/小时，并解锁 GraphQL 评审数据 | **强烈建议配置** |
| `LLM_*` | 任意 OpenAI 兼容接口 | `ask` 与网页版需要 |
| `HTTPS_PROXY` | 访问 GitHub 需要代理时填本地代理地址 | 可选 |

自检与测试：

```bash
python -m devprism doctor
python -m unittest discover -s tests -t . -v
```

---

## 它输出什么

以 `pallets/click` 为例（180 天窗口，节选）：

> | 指标 | 数值 | 评价 |
> | --- | --- | --- |
> | 提交总数 | 311 | 周均 12.1 |
> | 合并 PR | 122 | 周均 4.7，未合并 5 |
> | PR 合并周期（中位） | 22.3 小时 | 良好 |
> | 巴士因子 | 2 人 | 正常 |
> | 规范提交占比 | 3% | 关注 |
>
> **[中] 夜间提交占比偏高** — 38% 的提交发生在 22:00–05:59，可能意味着排期过紧或线上救火频繁。

Java 仓库会额外输出一节工程结构，例如 `google/gson`：Maven、Java 11、121 个主代码类 / 142 个测试类、7 个子模块、根 POM 声明 3 个依赖、6 个 CI 配置文件。

用 `python -m devprism analyze owner/repo` 可复现；一份完整的周报样例见 [`examples/sample-weekly-report.md`](examples/sample-weekly-report.md)。

---

## 三个关键设计决策

### 1. 指标引擎不依赖大模型

指标计算是纯函数，可以离线运行、独立单测。大模型只负责"决定查什么"和"把数据讲成人话"——把不可测的部分压缩到最小。指标算错了是工程问题，能被测试抓住；只有表述层才需要模型。

### 2. 缺字段 ≠ 指标为 0

GitHub 的 `/pulls` **列表**接口不返回 `comments` / `review_comments` 字段（只有单个 PR 详情才有）。如果写成 `pr.get("review_comments") or 0`，就会把"字段不存在"算成"评论数为 0"，进而得出：

> ❌ 「100% 的已合并 PR 未经任何评审」

这个结论对一个成熟开源项目一眼就是假的。正确做法是判定为"不可用"并明确声明，不用 0 冒充。

### 3. 不用 `/stats/contributors`

该接口返回全量周粒度数据，实测单仓库 2 MB 以上，既浪费配额又容易在弱代理下被截断。贡献者分布直接用已到手的提交列表计算——少一次请求，窗口口径还更准。

---

## 目录结构

```
devprism/
├── cache.py          # SQLite 响应缓存 + ETag
├── github.py         # REST + GraphQL 客户端（限流、分页、gzip、截断标记）
├── net.py            # HTTP 出口统一，支持可选代理
├── snapshot.py       # 时间窗口与采集结果的数据结构
├── metrics.py        # 指标纯函数 + 风险规则
├── repo_index.py     # 仓库知识索引 + Java 工程结构
├── knowledge.py      # Markdown 切分 + BM25 检索（字符二元组）
├── report.py         # Markdown 报告渲染
├── llm.py            # OpenAI 兼容客户端（零 SDK）
├── tools.py          # 8 个工具 + JSON Schema 校验
├── agent.py          # ReAct 循环 + 死循环护栏 + 引用白名单
├── workflow.py       # 工作流执行器：DAG、人工确认、断点续跑
├── yamlite.py        # 极简 YAML 子集解析器（免第三方依赖）
├── history.py        # 按仓库分组的历史会话
├── markdown_lite.py  # Markdown → HTML（离线可用，强制转义）
├── web.py            # FastAPI + SSE 控制台
├── config.py         # 环境变量 / .env
└── cli.py            # 命令行入口
web/index.html        # 单文件前端，无构建步骤、无 CDN
knowledge/            # 团队规范（提交规范 / 评审清单 / 分支策略）
workflows/weekly.yaml # 周报工作流定义
tests/                # 12 个测试文件，117 个用例
docs/                 # 需求文档、使用手册、发布说明
```

---

## 边界与声明

本工具基于 Git 提交与 PR 元数据，**不含** CI 失败率、缺陷数据与代码复杂度，因此 Revert 占比只是"变更失败率"的近似。结构化指标里的「测试比例」是测试类与主代码类的数量之比，不是行覆盖率。

**所有结论仅用于团队改进，不应用于个人绩效考核。**
