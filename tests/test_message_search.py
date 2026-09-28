"""Literal SQL Message search, shared entrypoints and bounded decoding."""
from contextlib import closing
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent.email_index import EmailIndex
from agent.main import parse_tool_command
from agent.messages import search_messages
from agent.messages.sources import QQSource, EmailSource, SOURCES
from agent.qq_sync import QQStore
from agent.tools import FileTools, TOOLS
from test_message_workflow import message
from test_message_read_layer import _make_row


class SearchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.qq = QQStore(self.base / 'qq.sqlite3')
        self.email = EmailIndex(self.base / 'email.sqlite3')
        for target, value in [('agent.qq_sync.DB_PATH', self.qq.path),
                              ('agent.email_index.INDEX_PATH', self.email.path)]:
            mock = patch(target, value)
            mock.start()
            self.addCleanup(mock.stop)
        item = message(1)
        item.content.update(text='MaxRL paper 50% a_b path\\file',
                            sender=dict(nickname='张三', card='卡片', user_id=987654),
                            conversation=dict(type='group', name='FoundationML', id='876543'),
                            future=dict(array=[dict(deeper='未来值'), 765432, True]))
        self.save([item])
        self.email.write(dict(version=1, next_id=3, emails=[_make_row(
            id=2, subject='MaxRL subject', **{'from': 'unique@sender.test'},
            date='Mon, 28 Sep 2026 01:00:00 +0000', extra=dict(nested='额外邮件字段'))]))

    def save(self, items):
        with closing(self.qq.connect()) as db, db:
            db.execute('DELETE FROM messages')
            db.executemany('INSERT INTO messages VALUES (?, ?)',
                           [(str(m.id), json.dumps(asdict(m))) for m in items])

    def test_qq_leaf_values(self):
        for query in ('MaxRL paper', '张三', '卡片', '987654', 'FoundationML', '876543',
                      '未来值', '765432', 'true'):
            with self.subTest(query=query):
                self.assertEqual([m['id'] for m in search_messages(query, 'qq')['messages']], [1])

    def test_email_leaf_values(self):
        for query in ('MaxRL subject', 'unique@sender.test', '额外邮件字段'):
            with self.subTest(query=query):
                self.assertEqual(search_messages(query, 'email')['count'], 1)

    def test_keys_and_containers_are_not_values(self):
        for query in ('nickname', 'future', 'deeper', 'sender', '{', '['):
            with self.subTest(query=query):
                self.assertEqual(search_messages(query, 'qq')['count'], 0)

    def test_sources_and_global_time_order(self):
        self.assertEqual([m['source'] for m in search_messages('MaxRL')['messages']], ['email', 'qq'])
        for source in ('qq', 'email'):
            result = search_messages('MaxRL', source)
            self.assertEqual([m['source'] for m in result['messages']], [source])
            self.assertEqual(result['source'], source)
            self.assertFalse(result['truncated'])
        self.assertIsNone(search_messages('MaxRL')['source'])

    def test_literals_and_injection(self):
        for query in ('%', '_', '\\', '50%', 'a_b', 'path\\file'):
            with self.subTest(query=query):
                self.assertEqual(search_messages(query)['count'], 1)
        for query in ("' OR 1=1 --", 'a%b', 'path_file'):
            self.assertEqual(search_messages(query)['count'], 0)

    def test_validation(self):
        for query in ('', '   ', '\n\t', None, 1, True, [], {}):
            with self.subTest(query=query), self.assertRaisesRegex(ValueError, 'query'):
                search_messages(query)
        for source in ('invalid', '', 3, []):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, '消息来源'):
                search_messages('x', source)

    def test_common_projection_and_future_registry(self):
        class FutureSource(QQSource):
            def listing_sql(self, alias):
                path, projection = super().listing_sql(alias)
                return path, f"SELECT 'future-source' AS source, 'projection-id' AS id, payload, stamp, 1 AS imported FROM ({projection})"

            @staticmethod
            def decode_listing(payload):
                item = QQSource.decode_listing(payload)
                item.source = 'future-source'
                return item
        with patch.dict(SOURCES, {'future-source': FutureSource}):
            for query in ('future-source', 'projection-id', 'true', '2026-09-28T00:00:00.000000+00:00'):
                with self.subTest(query=query):
                    self.assertEqual(search_messages(query, 'future-source')['count'], 1)
            self.assertEqual(search_messages('future-source')['count'], 1)

    def test_no_mutation_memory_or_full_list(self):
        before = self.qq.path.read_bytes(), self.email.path.read_bytes()
        with patch.object(QQSource, 'list', side_effect=AssertionError('full list')), \
             patch.object(EmailSource, 'list', side_effect=AssertionError('full list')), \
             patch.object(FileTools, 'write_memory', side_effect=AssertionError('Memory')), \
             patch('agent.messages.importing.import_message', side_effect=AssertionError('import')):
            result = FileTools.__new__(FileTools)._execute('search_messages', dict(query='MaxRL'))
        self.assertEqual(result['count'], 2)
        self.assertTrue(all(not m['imported'] for m in result['messages']))
        self.assertEqual(before, (self.qq.path.read_bytes(), self.email.path.read_bytes()))

    def test_large_dataset_only_decodes_returned_results(self):
        self.save([message(i) for i in range(1, 10006)])
        with patch.object(QQSource, 'list', side_effect=AssertionError('full list')), \
             patch.object(EmailIndex, 'read', side_effect=AssertionError('full list')), \
             patch('agent.messages.sources.json.loads', wraps=json.loads) as decode:
            result = search_messages('完整中文')
        self.assertEqual(decode.call_count, 100)
        self.assertEqual(result['count'], 100)
        self.assertTrue(result['truncated'])
        self.assertEqual([m['id'] for m in result['messages']], list(range(10005, 9905, -1)))
        self.assertIn('结果超过 100 条，仅显示最近 100 条，请使用更具体的关键词。', result['display'])

    def test_exact_100_not_truncated_and_summary_only(self):
        self.save([message(i) for i in range(1, 101)])
        result = search_messages('完整中文')
        self.assertEqual(result['count'], 100)
        self.assertFalse(result['truncated'])
        self.assertNotIn('content', result['messages'][0])

    def test_empty_registry_and_missing_stores(self):
        with patch.dict(SOURCES, {}, clear=True):
            self.assertEqual(search_messages('x')['count'], 0)
        with patch('agent.qq_sync.DB_PATH', self.base / 'absent.sqlite3'):
            self.assertEqual(search_messages('x', 'qq')['count'], 0)

    def test_commands_and_agent_dispatch(self):
        for command, arguments in [
            ('/search_messages MaxRL', dict(query='MaxRL')),
            ('/search_messages qq', dict(query='qq')),
            ('/search_messages --source qq "MaxRL paper"', dict(query='MaxRL paper', source='qq')),
            ('/search_messages --source email 作业', dict(query='作业', source='email')),
            ('/search_messages path\\file', dict(query='path\\file')),
        ]:
            with self.subTest(command=command):
                name, args = parse_tool_command(command)
                self.assertEqual((name, args), ('search_messages', arguments))
                self.assertEqual(FileTools.__new__(FileTools)._execute(name, args), search_messages(**arguments))
        for command in ('/search_messages', '/search_messages "   "', '/search_messages --source qq'):
            with self.assertRaises(ValueError):
                parse_tool_command(command)
        spec = next(t for t in TOOLS if t['name'] == 'search_messages')['input_schema']
        self.assertEqual(set(spec['properties']), {'query', 'source'})
        self.assertEqual(spec['required'], ['query'])
        self.assertIn('query', FileTools.__new__(FileTools)._execute('search_messages', {'query': ''})['error'])

    def test_terminal_command_is_executed_without_agent_or_memory(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from agent.main import main
        from test_email_memory import ScriptClient
        root = self.base / 'memory'
        root.mkdir()
        (root / 'AGENT.md').write_text('Test protocol', encoding='utf-8')
        files = FileTools(root, lambda changes: {}, lambda *args: 'no')
        self.addCleanup(files.policy.close)
        before = {p.name: p.read_bytes() for p in root.rglob('*.md')}
        client = ScriptClient([])
        client.model = 'test'
        with patch('sys.argv', ['agent.main']), \
             patch('agent.main.FileTools', return_value=files), \
             patch('agent.main.Client', return_value=client), \
             patch('agent.main.load_config', return_value={'ANTHROPIC_AUTH_TOKEN': 'test'}), \
             patch('builtins.input', side_effect=['/search_messages --source qq "MaxRL paper"', '/exit']), \
             patch('agent.main.run_turn', side_effect=AssertionError('Agent called')), \
             redirect_stdout(StringIO()) as output:
            self.assertEqual(main(), 0)
        self.assertIn('找到 1 条 Message', output.getvalue())
        self.assertEqual(before, {p.name: p.read_bytes() for p in root.rglob('*.md')})
