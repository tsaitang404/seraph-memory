#!/usr/bin/env python3
"""Seraph 记忆引擎自主迭代：健康检查 + 噪音自清理 + 语义关联检查

设计原则（2026-08-09 用户要求"记忆引擎自己迭代"）：
- 不是一次性清理，而是可周期性运行的自我维护机制
- 噪音判定用「图谱价值」智能标准，不用固定黑名单（固定规则永远漏）
- 低风险清理自动执行；高风险只报告不删（防误删 IP 等有效实体）
- 每次输出健康报告：实体/事实/关系统计 + 变更 + 待人工审查清单

用法:
    python3 self_heal.py                 # 完整检查 + 自动清理低风险噪音
    python3 self_heal.py --report-only   # 只报告不修改
    python3 self_heal.py --db PATH       # 指定记忆库（默认 ~/.hermes/memory_store.db）
    python3 self_heal.py --min-facts N   # 保留引用数 >= N 的实体（默认 1）

cron 建议（每日）:
    0 3 * * *  /opt/hermes-agent/venv/bin/python3 /data/code/seraph-memory/scripts/self_heal.py
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

# 确保能从仓库根导入 llm_extract.py（脚本在 scripts/ 下运行）
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ─────────────────────────────────────────────────────────────
# 噪音判定（LLM 智能判断，非正则/非黑名单）
# ─────────────────────────────────────────────────────────────
# 原则（2026-08-11 用户纠正）：判断噪音不能用正则——正则永远不会完善，
# 永远有规则外的东西漏进去。必须结合名字 + 类型 + 关联事实内容智能判断。
# LLM 判断失败的实体一律保留（宁可不删，不可误删）。


def is_noise_entity(name: str, entity_type: str, fe_count: int, er_count: int) -> tuple[bool, str]:
    """判断实体是否噪音。返回 (是否噪音, 原因)。

    已废弃正则路径判定，改用 classify_noise_llm 批量智能判断。
    此函数保留仅为兼容签名；主流程不再调用。
    """
    return False, ""


def check_db(db_path: str, report_only: bool, min_facts: int, apply: bool = False) -> dict:
    """执行健康检查。返回报告 dict。

    apply=True 时执行自动清理（低风险噪音 + 孤立非 IP + LLM 类型修正）；
    默认只报告不修改。
    """
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    report: dict = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "db": db_path,
        "stats": {},
        "noise_removed": [],
        "orphans_removed": [],
        "type_fixes": [],
        "type_suggestions": [],
        "needs_review": [],
        "relations_check": {},
    }

    # ── 统计 ──
    report["stats"]["facts"] = con.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    report["stats"]["entities"] = con.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    report["stats"]["entity_relations"] = con.execute("SELECT COUNT(*) FROM entity_relations").fetchone()[0]
    report["stats"]["fact_entities"] = con.execute("SELECT COUNT(*) FROM fact_entities").fetchone()[0]
    report["stats"]["types"] = {
        r["entity_type"]: r["n"] for r in con.execute(
            "SELECT entity_type, COUNT(*) as n FROM entities GROUP BY entity_type ORDER BY n DESC"
        )
    }

    # ── 噪音实体检测（LLM 智能判断，结合名字+类型+关联事实）──
    # 增量审查：只查 reviewed=0（未审查/变化过的），已确认的跳过
    try:
        con.execute("SELECT reviewed FROM entities LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        con.execute("ALTER TABLE entities ADD COLUMN reviewed INTEGER DEFAULT 0")
        con.commit()

    rows = con.execute("""
        SELECT e.entity_id, e.name, e.entity_type, e.reviewed,
            (SELECT COUNT(*) FROM fact_entities fe WHERE fe.entity_id = e.entity_id) as fe,
            (SELECT COUNT(*) FROM entity_relations er WHERE er.source_entity = e.entity_id OR er.target_entity = e.entity_id) as er
        FROM entities e
        WHERE e.reviewed = 0
    """).fetchall()

    # 收集关联事实上下文（每个实体最多 3 条）
    facts_map: dict[str, list[str]] = {}
    for r in rows:
        fs = con.execute(
            """SELECT substr(f.content, 1, 120) as c FROM facts f
               JOIN fact_entities fe ON f.fact_id = fe.fact_id
               WHERE fe.entity_id = ? ORDER BY f.fact_id LIMIT 3""",
            (r["entity_id"],),
        ).fetchall()
        facts_map[r["name"]] = [x["c"] for x in fs]

    # LLM 批量噪音判定（分批，失败返回 {} → 全部保留）
    noise_map: dict[str, bool] = {}
    try:
        from llm_extract import classify_noise_llm
        noise_map = classify_noise_llm(
            [(r["name"], r["entity_type"], r["fe"], r["er"]) for r in rows],
            facts_map,
        )
    except Exception as e:
        report["relations_check"]["noise_llm_error"] = str(e)

    to_delete: list[int] = []
    keep_ids: list[int] = []       # LLM 判保留 → 标 reviewed=1（已审查确认）
    review_ids: list[int] = []     # LLM 判噪音但有引用 → 标 reviewed=1（已审过，等人工）
    for r in rows:
        judged = r["name"] in noise_map
        is_noise = noise_map.get(r["name"], False)
        reason = "LLM 判定噪音"
        if judged and not is_noise:
            keep_ids.append(r["entity_id"])  # 判保留 → 自动确认
            continue
        if is_noise:
            if r["fe"] > 0 or r["er"] > 0:
                # 有引用但 LLM 判噪音 → 人工审查（防误删；标 reviewed 避免每天重复报）
                review_ids.append(r["entity_id"])
                report["needs_review"].append({"id": r["entity_id"], "name": r["name"], "reason": reason, "fe": r["fe"], "er": r["er"]})
            else:
                # 零引用纯噪音 → 自动删
                to_delete.append(r["entity_id"])
                report["noise_removed"].append({"id": r["entity_id"], "name": r["name"], "reason": reason, "fe": r["fe"], "er": r["er"]})

    # 已审查的实体标记 reviewed=1（下次跳过）：判保留 + 待人工确认的都标
    marked_ids = keep_ids + review_ids
    if marked_ids:
        marks = ",".join("?" * len(marked_ids))
        con.execute(f"UPDATE entities SET reviewed = 1 WHERE entity_id IN ({marks})", marked_ids)
        con.commit()

    # ── 孤立实体（零引用零关系）──
    orphan_ids = [
        r["entity_id"] for r in con.execute("""
            SELECT e.entity_id FROM entities e
            WHERE NOT EXISTS (SELECT 1 FROM fact_entities fe WHERE fe.entity_id = e.entity_id)
              AND NOT EXISTS (SELECT 1 FROM entity_relations er WHERE er.source_entity = e.entity_id OR er.target_entity = e.entity_id)
        """)
    ]
    # 孤立实体如果是 IP 保留（用户偏好），其余删
    orphan_to_del = []
    for oid in orphan_ids:
        name = con.execute("SELECT name FROM entities WHERE entity_id=?", (oid,)).fetchone()[0]
        if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}(:\d+)?", name):
            continue  # IP 保留
        orphan_to_del.append(oid)
        report["orphans_removed"].append({"id": oid, "name": name})

    # ── 类型修正：IP unknown → resource ──
    ip_fix = con.execute("""
        SELECT entity_id, name FROM entities
        WHERE entity_type IN ('unknown', '') AND name GLOB '*.*.*.*'
    """).fetchall()
    for r in ip_fix:
        report["type_fixes"].append({"id": r["entity_id"], "name": r["name"], "from": "unknown", "to": "resource"})

    # ── 实体类型合理性检查（2026-08-09 asus 反馈：自迭代应检查类型是否合理）──
    # 用 LLM 智能分类（不用规则/type_map——规则不能穷尽，会重蹈固定黑名单覆辙）
    type_suggestion = []
    # 收集需要检查的实体：unknown 类型（数量大时限制，避免每轮全库 LLM 调用）
    check_rows = con.execute(
        "SELECT entity_id, name, entity_type FROM entities "
        "WHERE entity_type IN ('unknown', '') ORDER BY entity_id LIMIT 200"
    ).fetchall()
    if check_rows:
        # 收集每个实体的关联事实作上下文
        facts_map: dict[str, list[str]] = {}
        for r in check_rows:
            fs = con.execute(
                """SELECT substr(f.content, 1, 120) as c FROM facts f
                   JOIN fact_entities fe ON f.fact_id = fe.fact_id
                   WHERE fe.entity_id = ? ORDER BY f.fact_id LIMIT 3""",
                (r["entity_id"],),
            ).fetchall()
            facts_map[r["name"]] = [x["c"] for x in fs]
        try:
            from llm_extract import classify_entity_types_llm
            suggested = classify_entity_types_llm(
                [(r["name"], r["entity_type"]) for r in check_rows], facts_map
            )
        except Exception as e:
            suggested = {}
            report["relations_check"]["classify_error"] = str(e)
        for r in check_rows:
            new_type = suggested.get(r["name"])
            if new_type and new_type != r["entity_type"]:
                type_suggestion.append({
                    "id": r["entity_id"], "name": r["name"],
                    "from": r["entity_type"], "to": new_type,
                })
                report["type_suggestions"].append({
                    "id": r["entity_id"], "name": r["name"],
                    "from": r["entity_type"], "to": new_type,
                })

    # ── 执行类型修正（LLM 建议的类型自动应用；低风险）──
    if apply:
        for s in type_suggestion:
            con.execute("UPDATE entities SET entity_type=? WHERE entity_id=?", (s["to"], s["id"]))
            report["type_fixes"].append({"id": s["id"], "name": s["name"], "from": s["from"], "to": s["to"]})

    # ── 语义关联检查 ──
    # 孤立事实（无任何实体连接）
    orphan_facts = con.execute("""
        SELECT f.fact_id, substr(f.content, 1, 60) as content FROM facts f
        WHERE NOT EXISTS (SELECT 1 FROM fact_entities fe WHERE fe.fact_id = f.fact_id)
        ORDER BY f.fact_id LIMIT 20
    """).fetchall()
    report["relations_check"]["orphan_facts"] = [{"id": r["fact_id"], "content": r["content"]} for r in orphan_facts]
    # unknown 类型占比
    total = report["stats"]["entities"]
    unknown_n = report["stats"]["types"].get("unknown", 0)
    report["relations_check"]["unknown_ratio"] = round(unknown_n / total, 4) if total else 0

    # ── 执行清理（apply=True 才执行）──
    if apply:
        if to_delete:
            ph = ",".join("?" * len(to_delete))
            con.execute(f"DELETE FROM fact_entities WHERE entity_id IN ({ph})", to_delete)
            con.execute(
                f"DELETE FROM entity_relations WHERE source_entity IN ({ph}) OR target_entity IN ({ph})",
                to_delete + to_delete,
            )
            con.execute(f"DELETE FROM entities WHERE entity_id IN ({ph})", to_delete)
        if orphan_to_del:
            ph = ",".join("?" * len(orphan_to_del))
            con.execute(f"DELETE FROM fact_entities WHERE entity_id IN ({ph})", orphan_to_del)
            con.execute(
                f"DELETE FROM entity_relations WHERE source_entity IN ({ph}) OR target_entity IN ({ph})",
                orphan_to_del + orphan_to_del,
            )
            con.execute(f"DELETE FROM entities WHERE entity_id IN ({ph})", orphan_to_del)
        for r in ip_fix:
            con.execute("UPDATE entities SET entity_type='resource' WHERE entity_id=?", (r["entity_id"],))
        con.commit()

        # 清理后重新统计
        report["stats"]["entities_after"] = con.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        report["stats"]["unknown_after"] = con.execute(
            "SELECT COUNT(*) FROM entities WHERE entity_type='unknown'"
        ).fetchone()[0]

    con.close()
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="Seraph 记忆引擎自主健康检查/自清理")
    ap.add_argument("--db", default=str(Path.home() / ".hermes" / "memory_store.db"))
    ap.add_argument("--report-only", action="store_true", help="只报告不修改（默认行为）")
    ap.add_argument("--apply", action="store_true", help="执行自动清理（低风险噪音+孤立非IP+LLM类型修正）")
    ap.add_argument("--min-facts", type=int, default=1, help="引用数达到该值且名字可疑的实体转人工审查（默认 1）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（便于 cron 脚本消费）")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"数据库不存在: {args.db}", file=sys.stderr)
        return 2

    report = check_db(args.db, args.report_only, args.min_facts, apply=args.apply)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    # 人类可读报告
    s = report["stats"]
    print(f"═══ Seraph 记忆引擎自检 {report['ts']} ═══")
    print(f"库: {report['db']}")
    print(f"facts={s['facts']} entities={s.get('entities_after', s['entities'])} "
          f"relations={s['entity_relations']} fact_entities={s['fact_entities']}")
    if "unknown_after" in s:
        print(f"清理后 unknown={s['unknown_after']}")
    else:
        print(f"unknown 占比: {report['relations_check']['unknown_ratio']*100:.1f}%")
    print(f"\n噪音实体删除: {len(report['noise_removed'])}")
    for x in report["noise_removed"][:10]:
        print(f"  - {x['name']} ({x['reason']})")
    if len(report["noise_removed"]) > 10:
        print(f"  ... 等 {len(report['noise_removed'])} 个")
    print(f"孤立实体删除: {len(report['orphans_removed'])}")
    print(f"类型修正(IP→resource): {len(report['type_fixes'])}")
    print(f"类型建议(unknown→domain/server等): {len(report['type_suggestions'])}")
    for x in report["type_suggestions"][:8]:
        print(f"  ~ {x['name']}: {x['from']} → {x['to']}")
    print(f"\n待人工审查: {len(report['needs_review'])}")
    for x in report["needs_review"][:10]:
        print(f"  ⚠ {x['name']} ({x['reason']}, fe={x['fe']}, er={x['er']})")
    print(f"\n孤立事实(无实体连接): {len(report['relations_check']['orphan_facts'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
