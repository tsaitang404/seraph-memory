# Seraph Memory

**炽天使记忆** — fork 自 Holographic 记忆插件，增加 **LLM 实体提取层**。

## 为什么有 Seraph

Holographic 的实体提取只用**正则**（大写短语/引号/AKA），对以下场景完全失效：
- **中文名称**（如云存储服务名）
- **小写主机名**（如 `host-a`、`host-b`）
- **域名**（如 `example.com`）

Seraph 保留 Holographic 的全部优点（本地 SQLite、FTS5、HRR 向量、信任评分），
额外增加 **LLM 实体提取**：写入事实时调用 LLM 识别实体，正则识别不到的
中文/域名/小写名称也能正确建立实体关系。

## 安装

```bash
# 1. 克隆到 Hermes 插件目录
git clone https://github.com/your-name/seraph-memory ~/.hermes/plugins/seraph

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
无需额外配置。聚合类 provider 会自动回退到可用的 OpenAI 兼容 provider。

## 与 Holographic 的差异

| | Holographic | Seraph |
|---|---|---|
| 实体提取 | 正则（大写/引号）| 正则 + **LLM**（中文/域名/小写）|
| 实体类型 | 无（unknown）| LLM 分类（server/domain/service/...）|
| 实体关系 | 无 | LLM 提取三元组（A -rel-> B）|
| 实体描述 | 无 | LLM 生成结构化描述 |
| 标题 | 无（content 截断）| title 字段（Trilium 友好）|
| 存储 | SQLite | SQLite（同构，自动迁移）|
| 检索 | FTS5 + HRR | FTS5 + HRR（同构）|
| 信任评分 | 固定增量（+0.05/-0.10）| **递减制**（见下）|

## 置信度机制

**Seraph 用递减制信任评分**（diminishing returns），区别于 Holographic 的固定增量：

```
helpful   → trust += 0.05 * (1 - old_trust)    # 越接近 1 加得越少
unhelpful → trust -= 0.10 * old_trust           # 越接近 1 减得越狠
```

- **收益递减**：低置信事实快速爬升（0.5 → 0.7），高置信事实需要多次确认才微动
- **不会饱和到 100%**：数学上逼近 1.0 但达不到——事实永远保留"可被证伪"的空间
- **证伪惩罚重**：一条"确认过"的高置信事实被推翻，跌幅大，符合直觉
- **可信度可比较**：trust 0.9 vs 0.6 有实际区分度（固定增量会一堆事实卡在 1.0）
- 通过 `fact_feedback` 工具（helpful/unhelpful）或 `fact_store update`（trust_delta）调整

## 记忆服务（可选组件）

Seraph 除了作为 Hermes 插件，还可以作为**独立 HTTP 服务**运行（`api_server.py`），
让外部工具（笔记应用、脚本、其他 agent）把内容异步写入记忆引擎：

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/v1/memories` | 入队：`{note_id, title, content, tags}`，**202 即回** |
| GET | `/v1/queue/status` | 队列深度 / worker 状态 |
| GET | `/health` | 探活 |

**异步队列**：`pending_extractions` 表（与 facts 同库，WAL），`note_id UNIQUE` = 天然防抖
（重复编辑冲突 UPDATE，队列永远只有最新一条）；worker 线程后台消化 → LLM 实体/关系提取 → 写库。
重启自动恢复中断任务（processing → pending）；LLM 失败指数退避重试（3 次标 failed 留查）。
`note_map` 表存 note_id → fact_id 映射，同一笔记再编辑 = 更新原 fact（重建实体边）。

**Trilium 集成示例**（`trilium_memory_bridge.js`）：Trilium 事件脚本
（JS backend code note，mime 必须 `application/javascript;env=backend`），
`~runOnNoteContentChange`（isInheritable）挂图谱根，子树内笔记内容变化触发：
- 过滤：跳过 `#syncKey` 镜像节点（防回流循环）、`#memoryIgnore`（手动排除）
- 范围判定：祖先链含 `#hermesKnowledgeGraph` 标签（属性驱动，不硬编码 noteId）
- 事件语义：`api.currentNote` = 脚本自身，**被修改的笔记在 `api.originEntity`**

运行（需 Hermes venv：llm_extract 惰性 import hermes_cli 的 provider 配置）：

```bash
SERAPH_API_TOKEN=<token> /opt/hermes-agent/venv/bin/python3.12 api_server.py
# 默认监听 127.0.0.1:8787
```

依赖：fastapi + uvicorn（Hermes venv 安装）。鉴权 Bearer token 由环境变量提供，代码无硬编码密钥。

## 跟随上游

本仓库 fork 自 `NousResearch/hermes-agent` 的 `plugins/memory/holographic/`。
上游更新时同步：

```bash
git remote add upstream https://github.com/NousResearch/hermes-agent.git
git fetch upstream
git checkout upstream/main -- plugins/memory/holographic/  # 手动合并
```

## 许可证

同上游（MIT）。
