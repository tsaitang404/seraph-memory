#!/usr/bin/env python3
"""批量生成所有实体的 LLM 描述（Seraph）。

遍历 entities 表，对每个实体：
1. 收集关联事实（fact_entities → facts.content）
2. 收集关联关系（entity_relations，若存在）
3. LLM 生成描述
4. 写入 entities.description
"""
import os
import sys
import time
import sqlite3

HERMES_HOME = os.path.expanduser("~/.hermes")
MEMORY_DB = os.path.join(HERMES_HOME, "memory_store.db")

# 加载 .env 到环境（供 llm_extract 复用 Hermes 模型配置）
env_file = os.path.join(HERMES_HOME, ".env")
if os.path.exists(env_file):
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

sys.path.insert(0, "/data/code/seraph-memory")
from llm_extract import generate_entity_description

conn = sqlite3.connect(MEMORY_DB)
cur = conn.cursor()

# 检查 entity_relations 是否存在
has_relations = "entity_relations" in [
    r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
]

entities = cur.execute("SELECT entity_id, name FROM entities").fetchall()
print(f"共 {len(entities)} 个实体，开始生成描述...")

done = 0
skipped = 0
failed = 0

force = "--force" in sys.argv

for entity_id, name in entities:
    # 已有描述则跳过（--force 时重生成，用于旧格式画像升级到三行模板）
    row = cur.execute(
        "SELECT description FROM entities WHERE entity_id=? AND description != ''",
        (entity_id,),
    ).fetchone()
    if row and not force:
        skipped += 1
        continue
    if row and force:
        print(f"  ↻ 重生成 {name}（旧格式 → 三行模板）")

    # 收集关联事实
    facts = [
        r[0]
        for r in cur.execute(
            """
            SELECT f.content FROM facts f
            JOIN fact_entities fe ON f.fact_id = fe.fact_id
            WHERE fe.entity_id = ?
            """,
            (entity_id,),
        ).fetchall()
    ]

    # 收集关联关系
    relations = []
    if has_relations:
        relations = [
            (r[0], r[1], r[2])
            for r in cur.execute(
                """
                SELECT source_label, relation, target_label FROM entity_relations
                WHERE source_entity = ? OR target_entity = ?
                """,
                (entity_id, entity_id),
            ).fetchall()
        ]

    try:
        desc = generate_entity_description(name, facts, relations)
        if desc:
            cur.execute(
                "UPDATE entities SET description=? WHERE entity_id=?",
                (desc, entity_id),
            )
            conn.commit()
            done += 1
            print(f"  ✅ {name}: {desc[:50]}...")
        else:
            failed += 1
            print(f"  ⚠️ {name}: 生成失败（空）")
    except Exception as e:
        failed += 1
        print(f"  ❌ {name}: {e}")

    time.sleep(0.3)  # 避免 API 限流

print(f"\n完成: {done} 生成, {skipped} 跳过, {failed} 失败")
conn.close()
