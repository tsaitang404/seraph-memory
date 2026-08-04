#!/usr/bin/env python3
"""批量回填存量事实（Seraph 迁移后）：
1. 为无 title 的事实生成 title（_auto_title）
2. 为每条事实跑 LLM 实体提取 + 关系提取（补充 entity_relations）
3. 更新实体描述（关系变化后）
"""
import os
import sys
import time
import sqlite3

HERMES_HOME = os.path.expanduser("~/.hermes")
MEMORY_DB = os.path.join(HERMES_HOME, "memory_store.db")

# 加载 .env
env_file = os.path.join(HERMES_HOME, ".env")
if os.path.exists(env_file):
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

sys.path.insert(0, "/data/code/seraph-memory")
from llm_extract import extract_entities_llm, extract_relations_llm
from store import MemoryStore

store = MemoryStore(db_path=MEMORY_DB)
conn = store._conn
cur = conn.cursor()

# 1. 回填 title（无 title 的事实）
no_title = cur.execute("SELECT fact_id, content FROM facts WHERE title = '' OR title IS NULL").fetchall()
print(f"待回填 title: {len(no_title)} 条")
t_done = 0
for fid, content in no_title:
    t = store._auto_title(content)
    cur.execute("UPDATE facts SET title=? WHERE fact_id=?", (t, fid))
    t_done += 1
conn.commit()
print(f"  ✅ title 回填 {t_done} 条")

# 2. LLM 提取实体 + 关系（所有事实，幂等——entity_relations UNIQUE）
facts = cur.execute("SELECT fact_id, content FROM facts").fetchall()
print(f"\nLLM 提取 {len(facts)} 条事实...")
rel_total = 0
for i, (fid, content) in enumerate(facts):
    # 实体提取（LLM，补正则漏掉的）
    ents = extract_entities_llm(content)
    if ents:
        store._link_entities(fid, ents)
    # 关系提取
    rels = extract_relations_llm(content)
    if rels:
        n = store.add_relations(rels, fact_id=fid)
        rel_total += n
    if (i + 1) % 20 == 0:
        print(f"  ...{i+1}/{len(facts)}")
    time.sleep(0.2)

print(f"  ✅ 关系提取 {rel_total} 条")

# 3. 更新实体描述（关系变化后，重新生成有关系的实体描述）
print("\n更新实体描述（关系变化的实体）...")
import sqlite3 as _sq
try:
    _er_tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    if "entity_relations" in _er_tables:
        entities = cur.execute("""
            SELECT DISTINCT er.source_entity, e.name FROM entity_relations er
            JOIN entities e ON er.source_entity = e.entity_id
        """).fetchall()
    else:
        entities = []
except Exception:
    entities = []

from llm_extract import generate_entity_description
desc_done = 0
for entity_id, name in entities[:30]:
    facts_list = store.get_entity_facts(entity_id)
    rels_list = store.get_entity_relations(entity_id)
    desc = generate_entity_description(name, facts_list, rels_list)
    if desc:
        store.update_entity_description(entity_id, desc)
        desc_done += 1
    time.sleep(0.2)

print(f"  ✅ 实体描述更新 {desc_done} 个")

print("\n回填完成！")
conn.close()
