# 效能棱镜 DevPrism

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
├── tools.py          8 个工具 + JSON Schema 校验
├── workflow.py       YAML 工作流：DAG、人工确认、断点续跑
├── yamlite.py        极简 YAML 子集解析器
├── history.py        按仓库分组的历史
├── llm.py            OpenAI 兼容客户端
├── markdown_lite.py  Markdown → HTML，输出强制转义
├── web.py            FastAPI + SSE
├── config.py         环境变量 / .env
└── cli.py            命令行入口
web/index.html        单文件前端，没有构建步骤
knowledge/            团队规范样例
workflows/weekly.yaml 周报工作流
tests/                182 个用例
docs/                 需求、使用手册、发布说明
```

## 边界

数据只有 Git 提交和 PR 元数据，没有 CI 失败率、缺陷和代码复杂度，所以 Revert 占比只是"变更失败率"的近似。报告里的「测试比例」是测试类和主代码类的数量之比，不是行覆盖率。

另外，同一份目录树在代码分析里读不超过三个文件，那是导航和按需解释，不叫静态分析。

所有结论用于团队改进，不用于个人绩效考核。
