"""Database paging, exact ordering and one-time Email storage migration."""
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agent.email_index import EmailIndex
from agent.main import parse_tool_command
from agent.messages import list_messages, query_messages
from agent.qq_sync import QQStore
from agent.tools import FileTools, TOOLS
from test_message_read_layer import _make_row
from test_message_workflow import message


class PaginationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.qq = QQStore(base / 'qq.sqlite3')
        self.email = EmailIndex(base / 'email.sqlite3')
        for name, value in [('agent.qq_sync.DB_PATH', self.qq.path),
                            ('agent.email_index.INDEX_PATH', self.email.path)]:
            mocked = patch(name, value)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.populate(30)

    def populate(self, count):
        start = datetime(2026, 9, 1, tzinfo=timezone.utc)
        with closing(self.qq.connect()) as db, db:
            db.execute('DELETE FROM messages')
            db.executemany('INSERT INTO messages VALUES (?, ?)',
                           ((str(i), json.dumps(asdict(message(i, date=(start + timedelta(seconds=2*i)).isoformat()))))
                            for i in range(1, count + 1)))
        self.email.write(dict(version=1, next_id=count+1, emails=[
            _make_row(id=i, date=format_datetime(start + timedelta(seconds=2*i+1)))
            for i in range(1, count + 1)]))

    def test_default_limit_and_global_offset(self):
        expected = [(source, i) for i in range(30, 0, -1) for source in ('email', 'qq')]
        self.assertEqual([(m.source, m.id) for m in list_messages()], expected[:20])
        self.assertEqual([(m.source, m.id) for m in list_messages(limit=5)], expected[:5])
        for offset in (0, 10, 20, 49, 60, 1000):
            self.assertEqual([(m.source, m.id) for m in list_messages(limit=10, offset=offset)],
                             expected[offset:offset+10])

    def test_each_source_three_pages(self):
        for source in ('qq', 'email'):
            ids = []
            for offset in (0, 10, 20):
                page = list_messages(source, limit=10, offset=offset)
                self.assertTrue(all(m.source == source for m in page))
                self.assertEqual([m.id for m in page], list(range(30-offset, 20-offset, -1)))
                ids.extend(m.id for m in page)
            self.assertEqual(len(set(ids)), 30)

    def test_boundaries_errors_and_display(self):
        self.assertEqual(len(list_messages(limit=1)), 1)
        self.assertEqual(len(list_messages(limit=100)), 60)
        for limit in (0, -1, 101, True, 1.5, '5', None):
            with self.subTest(limit=limit), self.assertRaisesRegex(ValueError, 'limit'):
                list_messages(limit=limit)
        for offset in (-1, True, 1.5, '5', None, 2**63):
            with self.subTest(offset=offset), self.assertRaisesRegex(ValueError, 'offset'):
                list_messages(offset=offset)
        with self.assertRaisesRegex(ValueError, '不支持的消息来源'):
            list_messages('invalid')
        result = query_messages(limit=5, offset=10)
        self.assertEqual((result['count'], result['offset'], result['limit']), (5, 10, 5))
        self.assertIn('当前返回 5 条消息，offset=10', result['display'])

    def test_time_precision_huge_ids_and_unknown_dates(self):
        ids = [10**76+1, 10**76+2, 10**77]
        fixtures = [message(ids[0], date='2026-09-28T08:00:00.000001+08:00'),
                    message(ids[1], date='2026-09-28T00:00:00.000001Z'),
                    message(ids[2], date='2026-09-28T00:00:00Z'),
                    message(4, date='invalid'), message(5, date=None),
                    message(6, date='2026-09-28T00:00:00')]
        with closing(self.qq.connect()) as db, db:
            db.execute('DELETE FROM messages')
            db.executemany('INSERT INTO messages VALUES (?, ?)',
                           [(str(m.id), json.dumps(asdict(m))) for m in fixtures])
        self.assertEqual([m.id for m in list_messages('qq')], [ids[1], ids[0], ids[2], 6, 5, 4])
        self.assertEqual([m.id for m in list_messages('qq', time_from='2026-09-28T00:00:00.000001Z',
                                                    time_to='2026-09-28T08:00:00.000001+08:00')],
                         [ids[1], ids[0]])

    def test_filters_are_before_offset(self):
        data = self.email.read()
        for row in data['emails']:
            row['imported'] = row['id'] % 2 == 0
            row['folder'] = 'Inbox' if row['id'] > 10 else 'Other'
        self.email.write(data)
        self.assertEqual([m.id for m in list_messages('email', imported=True, conversation='Inbox',
                                                    limit=3, offset=2)], [26, 24, 22])
        self.assertEqual(len(list_messages('qq', conversation='group:20', limit=5)), 5)
        self.assertEqual(len(list_messages('qq', conversation='20', limit=5)), 5)
        self.assertEqual(len(list_messages('qq', conversation='测试群', limit=5)), 5)
        self.assertEqual(list_messages('qq', conversation="' OR 1=1 --"), [])

    def test_email_timezone_unknown_date_and_message_id_filter(self):
        rows = [_make_row(id=1, date='Mon, 28 Sep 2026 08:00:00 +0800'),
                _make_row(id=2, date='Mon, 28 Sep 2026 00:00:00 +0000'),
                _make_row(id=3, date='invalid'),
                _make_row(id=4, date='Mon, 28 Sep 2026 00:00:00 -0000')]
        self.email.write(dict(version=1, next_id=5, emails=rows))
        self.assertEqual([m.id for m in list_messages('email')], [2, 1, 4, 3])
        self.assertEqual([m.id for m in list_messages('email', conversation='<abc@example.com>',
                                                    time_from='2026-09-28T00:00:00Z',
                                                    time_to='2026-09-28T00:00:00Z')], [2, 1])

    def test_equal_time_and_id_cross_source_tie_is_stable(self):
        item = message(1, date='2026-09-28T00:00:00Z')
        with closing(self.qq.connect()) as db, db:
            db.execute('DELETE FROM messages')
            db.execute('INSERT INTO messages VALUES (?, ?)', ('1', json.dumps(asdict(item))))
        self.email.write(dict(version=1, next_id=2, emails=[
            _make_row(id=1, date='Mon, 28 Sep 2026 00:00:00 +0000')]))
        self.assertEqual(list_messages(limit=1)[0].source, 'email')
        self.assertEqual(list_messages(limit=1, offset=1)[0].source, 'qq')

    def test_10005_per_source_decode_only_page_and_sql_limit(self):
        self.populate(10005)
        real_connect = sqlite3.connect
        statements = []

        def traced_connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection

        # Both old full-list paths are forbidden, and JSON decoding is counted independently.
        with patch.object(QQStore, 'list', side_effect=AssertionError('full QQ read')), \
             patch.object(EmailIndex, 'read', side_effect=AssertionError('full Email read')), \
             patch.object(EmailIndex, '_read_legacy', side_effect=AssertionError('repeated migration')), \
             patch('agent.messages.pagination.sqlite3.connect', side_effect=traced_connect), \
             patch('agent.messages.sources.json.loads', wraps=json.loads) as decode:
            for source in ('qq', 'email', None):
                decode.reset_mock()
                statements.clear()
                result = list_messages(source, limit=20, offset=5000)
                self.assertEqual(len(result), 20)
                self.assertEqual(decode.call_count, 20)
                queries = [sql for sql in statements if sql.startswith('SELECT source, payload')]
                self.assertEqual(len(queries), 1)
                self.assertIn('LIMIT 20 OFFSET 5000', queries[0])
                self.assertIn('ORDER BY', queries[0])
                if source is not None:
                    self.assertEqual([m.id for m in result], list(range(5005, 4985, -1)))

    def test_commands_and_shared_tool(self):
        for command, expected in [('/list_messages', {}), ('/list_messages qq', dict(source='qq')),
                                  ('/list_messages qq 5', dict(source='qq', limit=5)),
                                  ('/list_messages qq 5 10', dict(source='qq', limit=5, offset=10)),
                                  ('/list_messages {"limit":5,"offset":10}', dict(limit=5, offset=10)),
                                  ('/list_messages email {"limit":5,"offset":10}', dict(source='email', limit=5, offset=10))]:
            name, arguments = parse_tool_command(command)
            self.assertEqual((name, arguments), ('list_messages', expected))
            self.assertEqual(FileTools.list_messages(object(), **arguments), query_messages(**expected))
            tool_instance = FileTools.__new__(FileTools)
            self.assertEqual(tool_instance._execute(name, arguments), query_messages(**expected))
        self.assertEqual(tool_instance._execute('list_messages', {'source': None}), query_messages())
        self.assertIn('limit', tool_instance._execute('list_messages', {'limit': 101})['error'])
        self.assertIn('offset', tool_instance._execute('list_messages', {'offset': -1})['error'])
        for command in ('/list_messages qq 1 2 3', '/list_messages qq five',
                        '/list_messages qq {"source":"email"}'):
            with self.assertRaises(ValueError):
                parse_tool_command(command)
        tool = next(t for t in TOOLS if t['name'] == 'list_messages')['input_schema']
        self.assertEqual(tool['required'], [])
        self.assertIn('offset', tool['properties'])


class EmailMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.legacy = Path(temporary.name) / 'index.json'
        self.store = EmailIndex(self.legacy)

    def test_migrate_once_preserving_ids_state_and_original(self):
        huge = 10**77 + 123
        data = dict(version=1, next_id=huge+1,
                    emails=[_make_row(id=huge, imported=True, imported_at='2026-09-28')],
                    sync_progress={'mailbox': {'max_uid': 999}}, sync_failures=[{'error_type': 'test'}])
        self.legacy.write_text(json.dumps(data), encoding='utf-8')
        before = self.legacy.read_bytes()
        self.assertEqual(self.store.read(), data)
        self.assertEqual(self.legacy.read_bytes(), before)
        changed = dict(data, next_id=huge+2)
        with self.store.locked():
            self.store.write(changed)
        with patch.object(EmailIndex, '_read_legacy', side_effect=AssertionError('migration repeated')):
            self.assertEqual(EmailIndex(self.legacy).read(), changed)
        self.assertEqual(self.legacy.read_bytes(), before)

    def test_corrupt_legacy_is_preserved_and_can_retry(self):
        self.legacy.write_text('broken', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '格式损坏'):
            self.store.read()
        self.assertEqual(self.legacy.read_text(), 'broken')
        data = dict(version=1, next_id=2, emails=[_make_row()])
        self.legacy.write_text(json.dumps(data), encoding='utf-8')
        self.assertEqual(self.store.read(), data)

    def test_failed_migration_rolls_back_and_retries(self):
        data = dict(version=1, next_id=3, emails=[_make_row(), _make_row(id=2)])
        self.legacy.write_text(json.dumps(data), encoding='utf-8')
        original = EmailIndex._write_snapshot

        def fail(db, snapshot):
            original(db, snapshot)
            raise OSError('simulated interruption before commit')

        with patch.object(EmailIndex, '_write_snapshot', side_effect=fail):
            with self.assertRaises(OSError):
                self.store.read()
        self.assertEqual(self.store.read(), data)

    def test_snapshot_write_rolls_back(self):
        data = dict(version=1, next_id=2, emails=[_make_row()])
        self.store.write(data)
        with closing(self.store.connect()) as db, db:
            db.execute("CREATE TRIGGER fail_write BEFORE INSERT ON email_messages BEGIN SELECT RAISE(ABORT, 'disk error'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.write(dict(version=1, next_id=3, emails=[_make_row(id=2)]))
        self.assertEqual(self.store.read(), data)
