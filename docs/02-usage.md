# 使用手册

面向"想自己跑一遍"的场景。全部命令都在 **`F:\Project`** 目录下执行。

## 前置检查

打开 PowerShell：

```powershell
cd F:\Project
python -m devprism doctor
```

正常应该看到：

```
效能棱镜 环境自检
  GitHub Token : 已配置（****3ouk）
  GitHub API   : 可用，剩余 5000/5000 次/小时
  LLM API Key  : 已配置（****5544）
  LLM 连通性   : 正常（模型回复：'pong'）
```

任何一项显示「未配置」或「失败」，先去补 `.env`。

---

## 五个步骤，由浅入深

### ① 生成一份报告（不需要大模型）

```powershell
python -m devprism analyze pallets/click --days 180
```

产出 `report-pallets-click.md`。这一步只调 GitHub API 算指标，**不花钱**。

适合先确认整条数据链路通不通。

### ② 向 Agent 提问（需要大模型）

```powershell
python -m devprism ask "这个仓库的协作健康度怎么样？" pallets/click --days 180
```

屏幕上会滚出完整推理轨迹：

```
[思考 1] 我先获取仓库概览和协作健康度数据。
  → repo_overview({'owner': 'pallets', 'name': 'click'})
  → pull_health({'owner': 'pallets', 'name': 'click'})
结论：……
```

这一步每次消耗几千 token（DeepSeek 大概几分钱）。

### ③ 用仓库自己的规范来对照

```powershell
python -m devprism ask "提交规范执行得怎么样？" angular/angular --conventions repo
```

`--conventions repo` 会抓取目标仓库自己的 `CONTRIBUTING.md` 当规范。
对比试试 `pallets/click`——它不遵守 Conventional Commits，合规率只有 3%；
而 `angular/angular` 是这套规范的源头，合规率 98%。

### ④ 跑固定流程（工作流）

```powershell
python -m devprism run workflows/weekly.yaml --var owner=pallets --var name=click
```

会执行 4 步后在人工确认节点停下，打印草稿和 `run_id`。看过觉得没问题，续跑：

```powershell
python -m devprism run workflows/weekly.yaml --resume <上面打印的 run_id> --approve
```

产出 `report-pallets-click-weekly.md`。

**注意**：续跑不用再传 `--var`，输入已存在运行记录里。

### ⑤ 换成你自己的仓库

把 `pallets/click` 换成 `你的用户名/仓库名` 即可。你自己的仓库数据量小，
跑得更快，也更容易看出指标是否符合直觉。

### ⑥ 打开网页版

```powershell
pip install -e ".[web]"
python -m devprism serve
```

浏览器打开 `http://127.0.0.1:8800`，输入仓库地址点「开始分析」。
左边会实时滚出思考与工具调用，右边出结论。

只监听本机（`127.0.0.1`），别人访问不到。要换端口加 `--port 9000`。

---

## 命令速查

| 想做什么 | 命令 |
|---|---|
| 环境自检 | `python -m devprism doctor` |
| 出报告 | `python -m devprism analyze <owner>/<repo> --days 180` |
| 导出指标 JSON | `python -m devprism analyze <owner>/<repo> --json out.json` |
| 问 Agent | `python -m devprism ask "问题" <owner>/<repo>` |
| 用仓库自己的规范 | 加 `--conventions repo` |
| 跑工作流 | `python -m devprism run workflows/weekly.yaml --var owner=X --var name=Y` |
| 续跑工作流 | `python -m devprism run <yaml> --resume <run_id> --approve` |
| 网页版 | `python -m devprism serve` |
| 跑测试 | `python -m unittest discover -s tests -t .` |

常用参数：

- `--days N` 分析窗口，默认 180。窗口越小越快、越不容易被翻页截断。
- `--max-pages N` 单接口最大翻页数，默认 6。调大可拿到更多数据，但更慢。
- `--max-steps N` Agent 最大推理步数，默认 8。

---

## 预期耗时

| 场景 | 耗时 |
|---|---|
| 首次分析一个仓库 | 1–3 分钟（每次网络请求 10–20 秒） |
| 同一仓库再跑一次 | **几秒**（全走本地缓存） |
| Agent 回答一个问题 | 1–2 分钟（含 3–5 次模型调用） |
| 工作流完成 | 2–3 分钟 |

第一次慢是正常的，瓶颈在网络。**演示前先跑一遍预热缓存**，第二次就是秒出。

---

## 常见问题

**`No module named devprism`**
不在项目根目录。先 `cd F:\Project`。

**报告里出现「数据被截断」或「仅覆盖 X 天」**
仓库太活跃，翻页上限内没拿全。两种解法：调大 `--max-pages`，或缩小 `--days`。
这是如实标注，不是 bug——宁可说不知道，也不给错数。

**报告里出现「—」和「无数据」**
该仓库在这个窗口内确实没有对应数据（比如没有合并过 PR）。不是故障。

**答案里出现 `⚠未验证` 的引用**
模型引用了知识库里不存在的章节，被校验拦下了。属于预期行为，说明防护在起作用。

**`GitHub API 剩余 0`**
配额用尽。确认 `.env` 里的 `GITHUB_TOKEN` 有效，或者等一小时。

**`模型接口返回 401`**
`LLM_API_KEY` 不对或已过期，去控制台重新生成。

**中文乱码**
已在代码里强制 UTF-8。若仍然乱码，执行 `chcp 65001` 后再试。

---

## 花多少钱

- GitHub API：免费（配 Token 后 5000 次/小时）
- 模型：`analyze` 完全不调模型；`ask` 和 `run` 每次约几分钱

正常测试几十次，成本在一块钱以内。想省钱就用 `analyze` 和测试套件，它们不碰模型。
