#!/bin/bash
# soul Seraph 记忆引擎自检（每日）——2026-10-03 修复：对齐 minius 版三处
# (1) PYTHONPATH 注入 plugins/seraph（否则 llm_extract import 失败，LLM 判定静默失效）
# (2) --apply 才真删（否则纯报告，此前 28 次全是假删除）
# (3) noise_removed ∪ orphans_removed 按 id 并集计数（否则重复数）
export PYTHONPATH="/home/joy/.hermes/plugins/seraph${PYTHONPATH:+:$PYTHONPATH}"
OUT=$(/usr/bin/python3 /home/joy/.hermes/scripts/self_heal.py --db /home/joy/.hermes/memory_store.db --json --apply 2>&1)
if [ $? -ne 0 ]; then
    echo "⚠️ Seraph 自检脚本执行失败: $OUT"
    exit 1
fi
NOISE=$(echo "$OUT" | /usr/bin/python3 -c "
import json,sys
d=json.load(sys.stdin)
ids={x['id'] for x in d['noise_removed']} | {x['id'] for x in d['orphans_removed']}
print(len(ids))
" 2>/dev/null)
REVIEW=$(echo "$OUT" | /usr/bin/python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d['needs_review']))" 2>/dev/null)
ORPHAN=$(echo "$OUT" | /usr/bin/python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d['relations_check']['orphan_facts']))" 2>/dev/null)
if [ "${NOISE:-0}" -gt 0 ] || [ "${REVIEW:-0}" -gt 0 ] || [ "${ORPHAN:-0}" -gt 0 ]; then
    echo "Seraph 自检：删除噪音 ${NOISE}，待审查 ${REVIEW}，孤立事实 ${ORPHAN}"
    echo "$OUT" | /usr/bin/python3 -c "
import json, sys
d = json.load(sys.stdin)
for x in d['noise_removed']: print(f\"  🧹 删: {x['name']} ({x['reason']})\")
for x in d['needs_review'][:10]: print(f\"  ⚠️ 审: {x['name']} ({x['reason']}, fe={x['fe']})\")
"
fi
exit 0
