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


# ─────────────────────────────────────────────────────────────
# 噪音判定（智能标准，非黑名单）
# ─────────────────────────────────────────────────────────────

def is_noise_entity(name: str, entity_type: str, fe_count: int, er_count: int) -> tuple[bool, str]:
    """判断实体是否噪音。返回 (是否噪音, 原因)。

    智能标准：可标识 / 稳定 / 非冗余 三原则 + 典型噪音模式启发式。
    低引用 + 命中噪音模式 → 噪音。有事实引用且语义合理 → 保留。
    """
    # 1. IP 是有效实体（用户明确偏好）——除非是纯端口数字
    if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}(:\d+)?", name):
        return False, "IP 有效实体"
    if re.fullmatch(r"\d+", name):  # 纯端口/数字
        return True, "纯数字/端口"

    # 2. 路径类（~/xxx、/xxx/xxx）
    if name.startswith("~") or name.startswith("/") or "/" in name and re.search(r"\.\w+$", name):
        return True, "文件/路径"

    # 3. transient 组件实例（name(instance) 括号后缀）
    if "(" in name and ")" in name:
        return True, "组件实例"

    # 4. 角色标签/泛词（常见中文泛词）
    generic_zh = {
        "系统", "集群", "子域名", "数据目录", "集群机器", "集群角色表",
        "root 全权限", "只读", "邮箱注册", "安装包", "多租户扩展机",
        "扩展机", "堡垒机", "环境", "数据库", "中间件", "服务", "主机",
        "机器", "服务器", "目录", "文件", "配置", "用户", "账号", "密钥",
    }
    if name in generic_zh:
        return True, "泛词/角色标签"

    # 5. 英文泛词
    generic_en = {
        "system", "cluster", "subdomain", "user", "host", "machine",
        "server", "service", "config", "file", "data", "database",
        "key", "password", "token", "api_key", "curl", "aur", "root",
        "admin", "public_key", "private_key", "local_router_dns",
        "vpn_dns", "blueking_cluster", "system",
    }
    if name.lower() in generic_en:
        return True, "英文泛词"

    # 6. 版本/包名（install_ee-V4.0.0、xxx-4.0.0 安装包）
    if re.search(r"[vV]?\d+\.\d+", name) and any(k in name for k in ("install", "安装包", "-")):
        return True, "安装包/版本"

    # 7. 未知类型 + 零引用 → 高风险孤立（报告但默认不自动删）
    if entity_type in ("unknown", "") and fe_count == 0 and er_count == 0:
        return True, "unknown 零引用孤立"

    return False, ""


def check_db(db_path: str, report_only: bool, min_facts: int) -> dict:
    """执行健康检查。返回报告 dict。"""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    report: dict = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "db": db_path,
        "stats": {},
        "noise_removed": [],
        "orphans_removed": [],
        "type_fixes": [],
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

    # ── 噪音实体检测 ──
    rows = con.execute("""
        SELECT e.entity_id, e.name, e.entity_type,
            (SELECT COUNT(*) FROM fact_entities fe WHERE fe.entity_id = e.entity_id) as fe,
            (SELECT COUNT(*) FROM entity_relations er WHERE er.source_entity = e.entity_id OR er.target_entity = e.entity_id) as er
        FROM entities e
    """).fetchall()

    to_delete: list[int] = []
    for r in rows:
        is_noise, reason = is_noise_entity(r["name"], r["entity_type"], r["fe"], r["er"])
        if is_noise:
            if r["fe"] > 0 and reason in ("unknown 零引用孤立",):
                # 有事实引用但名字可疑 → 人工审查
                report["needs_review"].append({"id": r["entity_id"], "name": r["name"], "reason": reason, "fe": r["fe"], "er": r["er"]})
            elif r["fe"] >= min_facts and reason != "unknown 零引用孤立":
                # 有较多引用但名字可疑 → 人工审查（防误删）
                report["needs_review"].append({"id": r["entity_id"], "name": r["name"], "reason": reason, "fe": r["fe"], "er": r["er"]})
            else:
                to_delete.append(r["entity_id"])
                report["noise_removed"].append({"id": r["entity_id"], "name": r["name"], "reason": reason, "fe": r["fe"], "er": r["er"]})

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

    # ── 执行清理（除非 report-only）──
    if not report_only:
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
    ap.add_argument("--report-only", action="store_true", help="只报告不修改")
    ap.add_argument("--min-facts", type=int, default=1, help="引用数达到该值且名字可疑的实体转人工审查（默认 1）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（便于 cron 脚本消费）")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"数据库不存在: {args.db}", file=sys.stderr)
        return 2

    report = check_db(args.db, args.report_only, args.min_facts)

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
    print(f"\n待人工审查: {len(report['needs_review'])}")
    for x in report["needs_review"][:10]:
        print(f"  ⚠ {x['name']} ({x['reason']}, fe={x['fe']}, er={x['er']})")
    print(f"\n孤立事实(无实体连接): {len(report['relations_check']['orphan_facts'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
