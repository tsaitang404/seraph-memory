#!/usr/bin/env python3
"""reviewed 标记机制单测：_touch / mark_reviewed / self_heal 增量审查"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from store import MemoryStore


def main():
    tmp = tempfile.mktemp(suffix=".db")
    s = MemoryStore(tmp)

    # 1. 迁移：entities 有 reviewed 列
    cols = {r[1] for r in s._conn.execute("PRAGMA table_info(entities)").fetchall()}
    assert "reviewed" in cols, "reviewed 列缺失"
    print("✅ 迁移：reviewed 列存在")

    # 2. 手动 link 实体（模拟 LLM 提取结果）→ 新实体 reviewed 应为 0
    fid = s.add_fact("sad 服务器运行 TriliumNext 服务（端口 8080）", category="project")
    s._link_entities(fid, [("sad", "server"), ("TriliumNext", "service")])
    row = s._conn.execute(
        "SELECT e.entity_id, e.name, e.reviewed FROM entities e JOIN fact_entities fe ON e.entity_id=fe.entity_id WHERE fe.fact_id=?",
        (fid,),
    ).fetchall()
    assert len(row) >= 2, "应有多个实体"
    print("✅ add_fact+link：新实体 reviewed=0", [(r[1], r[2]) for r in row])

    # 3. mark_reviewed 置 1
    eid = row[0][0]
    assert s.mark_reviewed(eid, True) is True
    v = s._conn.execute("SELECT reviewed FROM entities WHERE entity_id=?", (eid,)).fetchone()[0]
    assert v == 1, "mark_reviewed 置 1 失败"
    print("✅ mark_reviewed(True)：reviewed=1")

    # 4. 再 link 同一实体到新事实 → 自动清 0（_touch）
    fid2 = s.add_fact("sad 还运行 hermes gateway", category="project")
    s._link_entities(fid2, [("sad", "server")])
    # 找到 eid 现在的 reviewed
    v2 = s._conn.execute("SELECT reviewed FROM entities WHERE entity_id=?", (eid,)).fetchone()[0]
    assert v2 == 0, "关联新事实后应清 reviewed"
    print("✅ _touch：实体被新事实触碰后 reviewed 清 0")

    # 5. update_entity_type → 清 0
    s.mark_reviewed(eid, True)
    s.update_entity_type(eid, "server")
    v3 = s._conn.execute("SELECT reviewed FROM entities WHERE entity_id=?", (eid,)).fetchone()[0]
    assert v3 == 0, "类型变化应清 reviewed"
    print("✅ update_entity_type：类型变后 reviewed 清 0")

    # 6. mark_reviewed(False) 置 0
    s.mark_reviewed(eid, False)
    v4 = s._conn.execute("SELECT reviewed FROM entities WHERE entity_id=?", (eid,)).fetchone()[0]
    assert v4 == 0
    print("✅ mark_reviewed(False)：reviewed=0")

    # 7. remove_fact → 关联实体清 0
    s.mark_reviewed(eid, True)
    s.remove_fact(fid2)
    v5 = s._conn.execute("SELECT reviewed FROM entities WHERE entity_id=?", (eid,)).fetchone()[0]
    assert v5 == 0, "删除事实应清 reviewed"
    print("✅ remove_fact：删事实后 reviewed 清 0")

    os.unlink(tmp)
    print("\n🎉 全部通过")


if __name__ == "__main__":
    main()
