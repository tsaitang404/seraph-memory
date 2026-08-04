#!/usr/bin/env python3
"""刷新有关系的实体的 LLM 描述（回填第 3 步）。
关系提取后，重新生成受影响实体的描述。
"""
import os
import sys
import time
import sqlite3

HERMES_HOME = os.path.expanduser("~/.hermes")
MEMORY_DB = os.path.join(HERMES_HOME, "memory_store.db")

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
from store import MemoryStore

store = MemoryStore(db_path=MEMORY_DB)
conn = store._conn
cur = conn.cursor()

# 有关系的实体（作为 source 或 target）
entities = cur.execute("""
    SELECT DISTINCT e.entity_id, e.name FROM entity_relations er
    JOIN entities e ON er.source_entity = e.entity_id OR er.target_entity = e.entity_id
""").fetchall()
print(f"有关系的实体: {len(entities)} 个，刷新描述...")

done = 0
for entity_id, name in entities:
    facts_list = store.get_entity_facts(entity_id)
    rels_list = store.get_entity_relations(entity_id)
    desc = generate_entity_description(name, facts_list, rels_list)
    if desc:
        store.update_entity_description(entity_id, desc)
        done += 1
        if done % 10 == 0:
            print(f"  ...{done}/{len(entities)}")
    time.sleep(0.2)

print(f"\n✅ 描述刷新 {done}/{len(entities)}")
conn.close()
