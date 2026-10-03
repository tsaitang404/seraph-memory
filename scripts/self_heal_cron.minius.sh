#!/bin/bash
# minius Seraph 记忆引擎自检（每日）——路径已适配 NixOS（原 sad /opt/hermes-agent/venv 已废）
PY=$(ls -d /nix/store/*python3-3.13*-env/bin/python3 2>/dev/null | head -1)
[ -z "$PY" ] && PY=/run/current-system/sw/bin/python3
# 关键：llm_extract.py 在 plugins/seraph/ 下，而 self_heal.py 只把它自己
# 的 parent.parent（~/.hermes）插进 sys.path → 不给 PYTHONPATH 就
# "No module named 'llm_extract'"，LLM 噪音判定/类型修正/待审查清单全部静默失效
# （2026-10-03 实测发现，此前每天报的"噪音"其实只有孤立实体路径在干活）。
export PYTHONPATH="/var/lib/hermes/.hermes/plugins/seraph${PYTHONPATH:+:$PYTHONPATH}"
OUT=$( "$PY" /var/lib/hermes/.hermes/scripts/self_heal.py --db /var/lib/hermes/.hermes/memory_store.db --json --apply 2>&1)
if [ $? -ne 0 ]; then
    echo "⚠️ Seraph 自检脚本执行失败: $OUT"
    exit 1
fi
# 去重：LLM 判噪音且零引用的实体会同时出现在 noise_removed 和 orphans_removed，
# 直接相加会重复计数（2026-10-03 实测：删了 2 个却报"删除噪音 3"）。按 id 并集计数。
NOISE=$(echo "$OUT" | "$PY" -c "
import json,sys
d=json.load(sys.stdin)
ids={x['id'] for x in d['noise_removed']} | {x['id'] for x in d['orphans_removed']}
print(len(ids))
" 2>/dev/null)
REVIEW=$(echo "$OUT" | "$PY" -c "import json,sys; d=json.load(sys.stdin); print(len(d['needs_review']))" 2>/dev/null)
ORPHAN=$(echo "$OUT" | "$PY" -c "import json,sys; d=json.load(sys.stdin); print(len(d['relations_check']['orphan_facts']))" 2>/dev/null)
if [ "${NOISE:-0}" -gt 0 ] || [ "${REVIEW:-0}" -gt 0 ] || [ "${ORPHAN:-0}" -gt 0 ]; then
    echo "Seraph 自检：删除噪音 ${NOISE}，待审查 ${REVIEW}，孤立事实 ${ORPHAN}"
    echo "$OUT" | "$PY" -c "
import json, sys
d = json.load(sys.stdin)
for x in d['noise_removed']: print(f\"  🧹 删: {x['name']} ({x['reason']})\")
for x in d['needs_review'][:10]: print(f\"  ⚠️ 审: {x['name']} ({x['reason']}, fe={x['fe']})\")
"
fi
exit 0
