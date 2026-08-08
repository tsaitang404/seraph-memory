// trilium-memory-bridge.js
// Trilium → Seraph 实时记忆通道
//
// 触发：图谱根（#hermesKnowledgeGraph）子树内笔记内容变化（含创建）
// 过滤：跳过 #syncKey 镜像节点（防循环）、#memoryIgnore（手动排除）
// 提交：POST http://127.0.0.1:8787/v1/memories（异步队列，note_id 去重 = 天然防抖）
//
// 部署：JS backend code note（mime application/javascript;env=backend）
//       挂 ~runOnNoteContentChange（isInheritable）到图谱根
// 事件语义：api.currentNote = 脚本自身，api.originEntity = 被修改的笔记（触发者）

const SERAPH_URL = 'http://127.0.0.1:8787/v1/memories';
const SERAPH_TOKEN = '<set-me>'; // 仅 localhost 入队权限（无读取能力）

function log(msg) {
    console.log('[memory-bridge] ' + msg);
}

async function submitNote(note) {
    try {
        // 1. 镜像节点 / 手动排除 → 跳过（防循环）
        if (note.hasLabel('syncKey') || note.hasLabel('memoryIgnore')) {
            return;
        }
        // 2. 必须在图谱根子树内（属性驱动，不硬编码 noteId）
        const inGraph = note.getAncestors().some((a) => a.hasLabel('hermesKnowledgeGraph'));
        if (!inGraph) {
            return;
        }
        // 2.5 配置镜像子树（skills/SOUL/记忆，hermes-trillium-sync 10s 写入）→ 跳过
        //     hermes-trillium-sync 在「Hermes配置」节点上挂了 #memoryIgnore（isInheritable），
        //     子节点 hasLabel 继承命中；这里再显式扫祖先链做双保险（防标签挂在非直接父级）。
        const inConfigMirror = note.getAncestors().some((a) => a.hasLabel('memoryIgnore'));
        if (inConfigMirror) {
            return;
        }
        // 3. 只收文本内容
        let content = '';
        try {
            content = note.getContent();
        } catch (e) {
            return; // 非文本笔记（render/code 等）跳过
        }
        if (typeof content !== 'string' || !content.trim()) {
            return;
        }
        // 4. 提交（202 即回，队列异步消化）
        const resp = await fetch(SERAPH_URL, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                Authorization: 'Bearer ' + SERAPH_TOKEN,
            },
            body: JSON.stringify({
                note_id: note.noteId,
                title: note.title || '',
                content: content,
                tags: '',
            }),
        });
        if (!resp.ok) {
            log('submit failed status=' + resp.status + ' note=' + note.noteId);
        }
    } catch (e) {
        log('error note=' + (note && note.noteId) + ' : ' + e.message);
    }
}

// 事件入口：originEntity 是触发者，currentNote 是脚本自身
(async () => {
    const origin = api.originEntity;
    const note = origin && origin.noteId ? origin : api.currentNote;
    if (note) {
        await submitNote(note);
    }
})();
