# 推送到 GitHub

目标：把项目发布出去，同时保证 `.env` 里的密钥一个字节都不上传。

## 一、先装 Git

这台机器上还没有 `git`。三选一：

```powershell
winget install --id Git.Git -e
```

或者去 https://git-scm.com/download/win 下载安装包，或者用 GitHub Desktop（图形界面，自带 git）。

装完**重开一个 PowerShell 窗口**，验证：

```powershell
git --version
```

然后配置身份（只需一次）：

```powershell
git config --global user.name "你的名字"
git config --global user.email "你的邮箱"
```

## 二、在 GitHub 上建空仓库

网页 → New repository → 填名字（建议 `devprism`）→ **不要勾** Add a README / .gitignore / license。

勾了会先有一个初始提交，和你本地冲突，还得多一步 `pull`。保持空仓库最省事。

## 三、推送前先自检

```powershell
cd F:\Project
python scripts/check_secrets.py
```

必须看到：

```
已检查 42 个文件
结果：干净，没有发现密钥泄漏
```

退出码不是 0 就停下，别推。

## 四、初始化并提交

```powershell
git init
git add .
git status
```

**在 `git status` 的输出里逐行确认两件事**：

1. 列表里**没有** `.env`
2. 列表里**没有** `.cache/`

如果能看到 `.env`，说明 `.gitignore` 没生效，先停下来查原因，不要 commit。

确认无误后：

```powershell
git commit -m "feat: 效能棱镜 v0.1 —— 研发效能分析 Agent"
```

## 五、推送

```powershell
git branch -M main
git remote add origin https://github.com/<你的用户名>/devprism.git
git push -u origin main
```

第一次推送会弹出浏览器让你登录 GitHub（Git for Windows 自带的 Credential Manager 负责），登录一次后面就不用再输。

如果它要密码，注意：**GitHub 不接受账号密码**，要用 Personal Access Token 代替密码。不过通常浏览器授权走完就不需要了。

## 六、推完再验一遍

打开 `https://github.com/<你的用户名>/devprism`，肉眼确认：

- 文件列表里没有 `.env`
- 没有 `.cache`
- `README.md`、`LICENSE`、`examples/` 都在

---

## 万一已经推上去了

**先吊销密钥，再处理历史。** 顺序不能反。

1. GitHub → Settings → Developer settings → Tokens → 删掉那个 token
2. 模型控制台 → 删掉那个 API Key
3. 重新生成，写回本地 `.env`

因为 git 历史是可追溯的：即使后面再删文件、再提交一次，`git log` 里仍然能翻出旧内容。密钥一旦进过远端，就当它已经泄漏。

改写历史（`git filter-repo` 或 BFG）能把内容从所有提交里抹掉，但操作复杂且有风险。**对个人项目来说，最快的做法是：吊销密钥 → 删掉远端仓库 → 重新建一个 → 重新推。**

---

## 之后每次提交前

养成习惯：

```powershell
python scripts/check_secrets.py
git add .
git commit -m "..."
git push
```

这个脚本只做两件事——比对 `.env` 里的密钥值有没有出现在别的文件、匹配常见的密钥格式。它挡不住所有情况，但能挡住绝大多数手滑。
