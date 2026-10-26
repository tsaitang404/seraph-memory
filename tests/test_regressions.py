"""Offline regressions: actual SQLite queries and graph writes, no LLM/API calls.

The Hermes WAL bootstrap is replaced with equivalent SQLite initialization so
these tests also run outside the Hermes host.
"""
import importlib.util
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from store import MemoryStore, _SCHEMA
from retrieval import FactRetriever

spec = importlib.util.spec_from_file_location('seraph_self_heal', ROOT / 'scripts/self_heal.py')
heal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(heal)

class OfflineStore(MemoryStore):
    def _init_db(self):
        self._conn.execute('PRAGMA journal_mode=WAL')
        self._conn.executescript(_SCHEMA)
        self._conn.execute('ALTER TABLE entities ADD COLUMN reviewed INTEGER DEFAULT 0')

class Regressions(unittest.TestCase):
    def setUp(self):
        self.description_patch = patch.object(MemoryStore, 'ensure_entity_descriptions')
        self.description_patch.start()
        self.addCleanup(self.description_patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'memory.db'
        self.store = OfflineStore(self.path)
        self.search = FactRetriever(self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_maintenance_scripts_have_no_import_side_effects(self):
        for name in ['backfill', 'batch_describe', 'refresh_descriptions']:
            with self.subTest(script=name):
                spec = importlib.util.spec_from_file_location('import_check_' + name, ROOT / (name + '.py'))
                module = importlib.util.module_from_spec(spec)
                with patch('sqlite3.connect', side_effect=AssertionError('import opened database')):
                    spec.loader.exec_module(module)
                self.assertTrue(callable(module.main))

    def test_mixed_script_and_chinese_recall(self):
        fixtures = [('soul 使用 NixOS 配置', 'soul的配置'),
                    ('Cloudflare 的认证通过服务令牌', 'Cloudflare的配置'),
                    ('Trilium 管理笔记并与服务同步', 'Trilium笔记同步'),
                    ('记忆引擎采用 Seraph', '我的记忆引擎是什么')]
        for content, query in fixtures:
            fid = self.store.add_fact(content)
            with self.subTest(query=query):
                self.assertIn(fid, [r['fact_id'] for r in self.search.search(query)])

    def test_mixed_query_keeps_explicit_subject(self):
        wanted = self.store.add_fact('Cloudflare 服务访问令牌')
        self.store.add_fact('另一台服务器的配置与配置说明')
        self.assertEqual([wanted], [r['fact_id'] for r in self.search.search('Cloudflare的配置')])

    def test_update_tool_refreshes_llm_only_for_content(self):
        # Execute the actual handler in isolation from Hermes registration.
        import ast
        import json
        tree = ast.parse((ROOT / '__init__.py').read_text())
        provider = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'SeraphMemoryProvider')
        handler = next(n for n in provider.body if isinstance(n, ast.FunctionDef) and n.name == '_handle_fact_store')
        namespace = {'json': json, 'is_truthy_value': lambda x: x is True,
                     'tool_error': lambda x: x}
        exec(compile(ast.Module(body=[handler], type_ignores=[]), '<actual handler>', 'exec'), namespace)
        from unittest.mock import Mock
        instance = types.SimpleNamespace(_store=self.store, _retriever=self.search,
                                         _config={'llm_extract': True}, _llm_extract_and_link=Mock())
        fid = self.store.add_fact('Old Fact')
        namespace['_handle_fact_store'](instance, {'action': 'update', 'fact_id': fid, 'content': 'New Fact'})
        instance._llm_extract_and_link.assert_called_once_with(fid, 'New Fact')
        instance._llm_extract_and_link.reset_mock()
        namespace['_handle_fact_store'](instance, {'action': 'update', 'fact_id': fid, 'tags': 'new'})
        instance._llm_extract_and_link.assert_not_called()
        instance._config['llm_extract'] = False
        namespace['_handle_fact_store'](instance, {'action': 'update', 'fact_id': fid, 'content': 'Another Fact'})
        instance._llm_extract_and_link.assert_not_called()

    def test_filters_and_literals(self):
        low = self.store.add_fact('Cloudflare 记忆内容', category='private')
        self.store.update_fact(low, trust_delta=-0.4)
        self.assertEqual([], self.search.search('Cloudflare的配置'))
        self.store.add_fact('Cloudflare 公开配置', category='network')
        self.assertEqual([], self.search.search('Cloudflare的配置', category='private'))
        for query in ['', '  ', '不存在的词语', '%_的', '" OR * 的']:
            self.assertEqual([], self.search.search(query))
        self.assertEqual([], self.search.search('Cloudflare', limit=0))
        self.assertEqual([], self.search.search('Cloudflare', limit=-1))

    def test_english_fts_preserved(self):
        fid = self.store.add_fact('deployment rollback procedure')
        self.assertEqual(fid, self.search.search('what is the deployment rollback')[0]['fact_id'])

    def test_update_removes_only_old_fact_relations(self):
        first = self.store.add_fact('Old Service runs here')
        other = self.store.add_fact('Unchanged Service runs elsewhere')
        self.store.add_relations([('old', 'runs', 'service')], fact_id=first)
        self.store.add_relations([('other', 'runs', 'service')], fact_id=other)
        self.store._conn.execute('UPDATE entities SET reviewed=1')
        self.store.update_fact(first, content='New Engine runs elsewhere')
        relations = self.store._conn.execute('SELECT fact_id FROM entity_relations').fetchall()
        self.assertEqual([other], [r[0] for r in relations])
        self.assertEqual(0, self.store._conn.execute("SELECT reviewed FROM entities WHERE name='old'").fetchone()[0])
        self.assertFalse(self.store.update_fact(999999, content='nothing'))

    def test_metadata_update_preserves_graph(self):
        fid = self.store.add_fact('Stable Service')
        self.store.add_relations([('host', 'runs', 'service')], fact_id=fid)
        self.store.update_fact(fid, tags='confirmed')
        self.assertEqual(1, self.store._conn.execute('SELECT count(*) FROM entity_relations').fetchone()[0])

    def test_delete_cleans_relations_and_note_mapping(self):
        fid = self.store.add_fact('Removed Service')
        self.store.add_relations([('host', 'runs', 'service')], fact_id=fid)
        self.store._conn.execute('CREATE TABLE note_map(note_id TEXT PRIMARY KEY, fact_id INTEGER)')
        self.store._conn.execute('INSERT INTO note_map VALUES (?, ?)', ('note', fid))
        self.store.remove_fact(fid)
        self.assertEqual(0, self.store._conn.execute('SELECT count(*) FROM entity_relations').fetchone()[0])
        self.assertEqual(0, self.store._conn.execute('SELECT count(*) FROM note_map').fetchone()[0])

    def _heal(self, report_only, apply):
        fake = types.ModuleType('llm_extract')
        fake.classify_noise_llm = lambda rows, facts: {r[0]: r[0] == 'questionable' for r in rows}
        fake.classify_entity_types_llm = lambda rows, facts: {}
        with patch.dict(sys.modules, {'llm_extract': fake}):
            return heal.check_db(str(self.path), report_only, 1, apply=apply)

    def test_report_is_readonly_even_with_apply(self):
        self.store.add_fact('Some Service')
        self.store._link_entities(1, [('questionable', 'service')])
        before = list(self.store._conn.iterdump())
        report = self._heal(True, True)
        self.assertTrue(report['needs_review'])
        self.assertEqual(before, list(self.store._conn.iterdump()))
        self._heal(False, False)
        self.assertEqual(before, list(self.store._conn.iterdump()))

    def test_pending_review_remains_pending_on_apply(self):
        fid = self.store.add_fact('Some Service')
        self.store._link_entities(fid, [('questionable', 'service')])
        self._heal(False, True)
        self.assertEqual(0, self.store._conn.execute("SELECT reviewed FROM entities WHERE name='questionable'").fetchone()[0])
        self.assertTrue(self._heal(False, False)['needs_review'])

    def test_report_handles_legacy_schema_without_migration(self):
        self.store._conn.execute('ALTER TABLE entities DROP COLUMN reviewed')
        before = list(self.store._conn.iterdump())
        self._heal(True, False)
        self.assertEqual(before, list(self.store._conn.iterdump()))

if __name__ == '__main__':
    unittest.main()
