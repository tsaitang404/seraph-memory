# Seraph Memory

**炽天使记忆** — fork 自 Holographic 记忆插件，增加 **LLM 实体提取层**。

## 为什么有 Seraph

Holographic 的实体提取只用**正则**（大写短语/引号/AKA），对以下场景完全失效：
- **中文**（百度网盘、天翼云）
- **小写主机名**（sad、silk）
- **域名**（tm.aketer.me、poto.aketer.me）

Seraph 保留 Holographic 的全部优点（本地 SQLite、FTS5、HRR 向量、信任评分），
额外增加 **LLM 实体提取**：写入事实时调用 LLM 识别实体，正则识别不到的
中文/域名/小写名称也能正确建立实体关系。

## 安装

```bash
# 1. 克隆到 Hermes 插件目录
git clone https://github.com/tsaitang404/seraph-memory ~/.hermes/plugins/seraph

# 2. 启用（替换 holographic）
hermes memory setup   # 选择 seraph

# 3. 启用 LLM 提取
# 在 ~/.hermes/config.yaml:
plugins:
  seraph:
    db_path: $HERMES_HOME/memory_store.db
    llm_extract: true
```

## 配置项

| 键 | 默认 | 说明 |
|---|---|---|
| `db_path` | `$HERMES_HOME/memory_store.db` | SQLite 数据库路径 |
| `auto_extract` | `false` | 会话结束自动提取事实 |
| `default_trust` | `0.5` | 新事实默认信任分 |
| `hrr_dim` | `1024` | HRR 向量维度 |
| `llm_extract` | `false` | 启用 LLM 实体提取 |

**LLM 提取复用 Hermes 模型配置**（`model.default` + `model.provider` + provider API key），
无需额外配置。opencode-go 等 aggregator 自动回退到 deepseek。

## 与 Holographic 的差异

| | Holographic | Seraph |
|---|---|---|
| 实体提取 | 正则（大写/引号）| 正则 + **LLM**（中文/域名/小写）|
| 存储 | SQLite | SQLite（同构）|
| 检索 | FTS5 + HRR | FTS5 + HRR（同构）|
| 信任评分 | ✅ | ✅（同构）|

## 跟随上游

本仓库 fork 自 `NousResearch/hermes-agent` 的 `plugins/memory/holographic/`。
上游更新时同步：

```bash
git remote add upstream https://github.com/NousResearch/hermes-agent.git
git fetch upstream
git checkout upstream/main -- plugins/memory/holographic/  # 手动合并
```
