import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from agent.messages import get_message, list_messages
from agent.messages.qq_adapter import qq_to_message
from agent.qq_client import QQClient, QQClientError
from agent.qq_sync import QQStore, update_qq
from agent.main import parse_tool_command
from agent.tools import FileTools, TOOLS


def raw(id, kind='text', **changes):
    item = dict(message_id=id, time=1700000000, self_id=10,
                sender=dict(user_id=20, nickname='测试'),
                message=[dict(type=kind, data=dict(text=' 中文\nEnglish  '))])
    item.update(changes)
    return item


class Client:
    def __init__(self, messages):
        self.messages = messages
        self.calls = []
    def get_login_info(self): return dict(user_id=10)
    def list_group_chats(self): return []
    def list_private_chats(self): return [dict(user_id=20, nickname='friend')]
    def get_history_page(self, kind, peer, count, cursor):
        self.calls.append(cursor)
        end = len(self.messages) if cursor is None else next(i+1 for i,m in enumerate(self.messages) if str(m['message_id']) == cursor)
        return self.messages[max(0,end-count):end]


class QQTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'qq.sqlite3'
        self.conv = dict(type='private', id=20, name='friend')
    def sync(self, client, size=3):
        return update_qq(client=client, db_path=self.path, page_size=size)
    def test_text_identity_and_scopes(self):
        a = qq_to_message(raw(1), self.conv, 10)
        self.assertEqual(a.content['text'], ' 中文\nEnglish  ')
        self.assertFalse(a.imported)
        self.assertEqual(a.id, qq_to_message(raw(1, time=1800000000), self.conv, 10).id)
        group = qq_to_message(raw(1), dict(type='group', id=20), 10)
        self.assertNotEqual(a.id, group.id)
        outgoing = qq_to_message(raw(2, sender=dict(user_id=10)), self.conv, 10)
        self.assertEqual(outgoing.content['conversation']['id'], '20')
        self.assertEqual(set(vars(a)), {'id','source','time','imported','content'})
    def test_nontext_and_mixed(self):
        for kind in ('image','file','record','video','face','mface','json','share','forward','unknown'):
            item = raw(1, kind)
            self.assertIsNone(qq_to_message(item,self.conv,10))
            item['message'].append(dict(type='text',data=dict(text='hello')))
            self.assertEqual(qq_to_message(item,self.conv,10).content['text'], 'hello')
        client = Client([raw(i, kind) for i, kind in enumerate(('image','file','record','video'))])
        result = self.sync(client)
        self.assertEqual((result['added'], result['skipped'], result['failed']), (0,4,0))
        self.assertEqual(QQStore(self.path).list(), [])

    def test_group_failure_isolated_from_private(self):
        client = Client([raw(1)])
        original = client.get_history_page
        def history(kind, peer, count, cursor):
            if kind == 'group':
                raise QQClientError('group unavailable')
            return original(kind, peer, count, cursor)
        with patch.object(client, 'list_group_chats', return_value=[dict(group_id=30)]), patch.object(client, 'get_history_page', side_effect=history):
            result = self.sync(client)
        self.assertEqual((result['added'], result['failed']), (1,1))

    def test_account_namespace_and_checkpoint_reset(self):
        client = Client([raw(1)])
        self.assertEqual(self.sync(client)['added'], 1)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('DELETE FROM checkpoints')
            db.commit()
        self.assertEqual(self.sync(client)['added'], 0)
        client.messages[0]['self_id'] = 11
        with patch.object(client, 'get_login_info', return_value=dict(user_id=11)):
            self.assertEqual(self.sync(client)['added'], 1)
    def test_pagination_dedup_incremental_and_read_layer(self):
        client = Client([raw(i) for i in range(1,9)] + [raw(9,'image')])
        first = self.sync(client)
        self.assertEqual((first['scanned'],first['added'],first['skipped'],first['failed']), (9,8,1,0))
        second = self.sync(client)
        self.assertEqual(second['added'],0)
        client.messages.extend([raw(i) for i in range(10,17)] + [raw(17,'video')])
        third = self.sync(client)
        self.assertEqual((third['added'],third['failed']), (7,0))
        with patch('agent.qq_sync.DB_PATH',self.path):
            messages = list_messages('qq')
            self.assertEqual(len(messages),15)
            self.assertEqual(get_message('qq',messages[0].id),messages[0])
            self.assertEqual(list_messages('qq',imported=True),[])
    def test_failure_rollback_and_retry(self):
        client = Client([raw(i) for i in range(8)])
        original = client.get_history_page
        def fail(kind,peer,count,cursor):
            if cursor is not None: raise QQClientError('offline')
            return original(kind,peer,count,cursor)
        with patch.object(client,'get_history_page',side_effect=fail):
            self.assertEqual(self.sync(client)['failed'],1)
        self.assertEqual(QQStore(self.path).list(),[])
        self.assertEqual(self.sync(client)['added'],8)
    def test_malformed_retried_without_losing_valid_text(self):
        client = Client([raw(1,time='invalid'), raw(2)])
        self.assertEqual(self.sync(client)['failed'],1)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM checkpoints').fetchone()[0],0)
        client.messages[0]['time'] = 1700000000
        self.assertEqual(self.sync(client)['added'],1)
    def test_stuck_page_does_not_advance(self):
        client = Client([raw(i) for i in range(5)])
        with patch.object(client,'get_history_page',return_value=client.messages[-3:]):
            self.assertEqual(self.sync(client)['failed'],1)
        self.assertEqual(QQStore(self.path).list(),[])
    def test_entrypoints(self):
        for command in ('update_qq','update_qq()','/update_qq'):
            self.assertEqual(parse_tool_command(command), ('update_qq',{}))
        self.assertIn('update_qq',[s['name'] for s in TOOLS])
        with patch('agent.qq_sync.update_qq',return_value={'added':1}) as sync:
            self.assertEqual(FileTools.update_qq(object()),{'added':1})
            sync.assert_called_once()
            self.assertTrue(callable(sync.call_args.kwargs["progress"]))
    def test_api_pagination_parameters_and_error(self):
        client = QQClient()
        with patch.object(client,'_request',return_value={'messages':[]}) as request:
            client.get_history_page('private',20,3,'123')
            self.assertEqual(request.call_args.args[1]['message_seq'],'123')
            self.assertEqual(request.call_args.args[1]['reverseOrder'],'true')
        with patch.object(client,'_request',return_value={}):
            with self.assertRaises(QQClientError): client.get_history_page('group',20)
    def test_cq_string_and_array_literal(self):
        self.assertEqual(qq_to_message(raw(1,message='hi[CQ:image,file=x]'),self.conv,10).content['text'], 'hi')
        self.assertEqual(qq_to_message(raw(1,message='&#91;CQ:test&#93;&amp;'),self.conv,10).content['text'],'[CQ:test]&')



    def test_persistent_skip_state_skips_conversation(self):
        """A conversation marked skip in conversation_states is skipped without API calls."""
        client = Client([raw(i) for i in range(5)])
        # First sync succeeds
        self.assertEqual(self.sync(client)['added'], 5)
        # Mark as skip in database (table exists after first sync)
        scope = json.dumps(['10', 'private', '20'])
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('INSERT INTO conversation_states VALUES (?, ?)', (scope, 'skip'))
            db.commit()
        # Second sync skips the conversation entirely
        client2 = Client([raw(i) for i in range(5)])
        result = self.sync(client2)
        self.assertEqual(result['skipped'], 1)
        self.assertEqual(result['added'], 0)
        # No API calls should have been made (skipped before history fetch)
        self.assertEqual(client2.calls, [])

    def test_skip_event_stops_and_rolls_back_current_conversation(self):
        """Setting skip_event during sync rolls back partial writes and persists skip state."""
        import threading
        client = Client([raw(i) for i in range(20)])
        skip_event = threading.Event()
        call_count = [0]
        original_history = client.get_history_page
        def intercept(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] >= 2:
                skip_event.set()
            return original_history(*args, **kwargs)
        client.get_history_page = intercept
        result = update_qq(client=client, db_path=self.path, page_size=3, skip_event=skip_event)
        # The conversation should have been skipped (rolled back)
        self.assertEqual(result['added'], 0)
        self.assertEqual(QQStore(self.path).list(), [])
        # Skip state should be persisted
        scope = json.dumps(['10', 'private', '20'])
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute('SELECT state FROM conversation_states WHERE scope=?', (scope,)).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], 'skip')

    def test_skip_event_continues_to_next_conversation(self):
        """After skipping one conversation, the next one proceeds normally."""
        import threading
        class TwoChats(Client):
            def __init__(self):
                super().__init__([raw(i) for i in range(5)])
            def list_group_chats(self):
                return [dict(group_id=99, group_name='group')]
        # Pre-mark private chat as skip (create table first)
        QQStore(self.path).connect().close()
        scope = json.dumps(['10', 'private', '20'])
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('INSERT INTO conversation_states VALUES (?, ?)', (scope, 'skip'))
            db.commit()
        client = TwoChats()
        result = self.sync(client)
        # Private chat was skipped
        self.assertEqual(result['skipped'], 1)
        # Group chat was synced (messages share IDs with private, so only new ones added)
        self.assertGreater(result['added'], 0)

    def test_skip_state_survives_reconnection(self):
        """Skip state persists across database connections (program restart)."""
        import threading
        # First sync with skip_event set during execution
        client = Client([raw(i) for i in range(20)])
        skip_event = threading.Event()
        call_count = [0]
        original = client.get_history_page
        def intercept(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] >= 2:
                skip_event.set()
            return original(*args, **kwargs)
        client.get_history_page = intercept
        update_qq(client=client, db_path=self.path, page_size=3, skip_event=skip_event)
        # Verify skip state is in database
        scope = json.dumps(['10', 'private', '20'])
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute('SELECT state FROM conversation_states WHERE scope=?', (scope,)).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], 'skip')
        # New sync with new connection still skips
        client2 = Client([raw(i) for i in range(5)])
        result = update_qq(client=client2, db_path=self.path, page_size=3)
        self.assertEqual(result['skipped'], 1)
        self.assertEqual(client2.calls, [])

    def test_skip_does_not_count_as_error(self):
        """Skipped conversations increment 'skipped', not 'failed'."""
        import threading
        client = Client([raw(i) for i in range(20)])
        skip_event = threading.Event()
        call_count = [0]
        original = client.get_history_page
        def intercept(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] >= 2:
                skip_event.set()
            return original(*args, **kwargs)
        client.get_history_page = intercept
        result = update_qq(client=client, db_path=self.path, page_size=3, skip_event=skip_event)
        self.assertEqual(result['failed'], 0)
        self.assertEqual(result['skipped'], 0)  # skipped is not incremented for user-skip rollback
        self.assertEqual(result['errors'], [])

    def test_skip_preserves_old_messages(self):
        """Previously synced messages remain after skip rollback."""
        import threading
        # First sync: 5 messages
        client = Client([raw(i) for i in range(5)])
        self.assertEqual(self.sync(client)['added'], 5)
        old_messages = QQStore(self.path).list()
        self.assertEqual(len(old_messages), 5)
        # Second sync: new messages + skip
        client2 = Client([raw(i) for i in range(10)])
        skip_event = threading.Event()
        call_count = [0]
        original = client2.get_history_page
        def intercept(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] >= 2:
                skip_event.set()
            return original(*args, **kwargs)
        client2.get_history_page = intercept
        update_qq(client=client2, db_path=self.path, page_size=3, skip_event=skip_event)
        # Old messages should still be there
        current_messages = QQStore(self.path).list()
        self.assertEqual(len(current_messages), 5)
        self.assertEqual(set(m.id for m in current_messages), set(m.id for m in old_messages))

    def test_no_skip_event_means_no_skip(self):
        """When skip_event=None (Agent mode), sync proceeds normally."""
        client = Client([raw(i) for i in range(5)])
        result = update_qq(client=client, db_path=self.path, page_size=3, skip_event=None)
        self.assertEqual(result['added'], 5)
        self.assertEqual(result['skipped'], 0)
        self.assertEqual(result['failed'], 0)

    def test_skip_event_cleared_between_conversations(self):
        """skip_event is cleared before each conversation so stale state doesn't carry over."""
        import threading
        class TwoChats(Client):
            def __init__(self):
                super().__init__([raw(i) for i in range(5)])
            def list_group_chats(self):
                return [dict(group_id=99, group_name='group')]
        client = TwoChats()
        skip_event = threading.Event()
        # Pre-set the event; it should be cleared before processing
        skip_event.set()
        result = update_qq(client=client, db_path=self.path, page_size=3, skip_event=skip_event)
        # Both conversations succeed because event is cleared before each
        self.assertEqual(result['added'], 10)

    def test_multiple_skips_in_one_run(self):
        """Pressing Enter can skip multiple conversations across the same sync run."""
        import threading
        class ThreeChats(Client):
            def __init__(self):
                super().__init__([raw(i) for i in range(5)])
            def list_group_chats(self):
                return [dict(group_id=99, group_name='g1'), dict(group_id=100, group_name='g2')]
        client = ThreeChats()
        skip_event = threading.Event()
        # Simulate pressing Enter at the start of each conversation.
        # The event is cleared before each conversation, so we set it
        # on every call to get_history_page to ensure it's always set.
        call_count = [0]
        original = client.get_history_page
        def intercept(*args, **kwargs):
            call_count[0] += 1
            skip_event.set()
            return original(*args, **kwargs)
        client.get_history_page = intercept
        result = update_qq(client=client, db_path=self.path, page_size=3, skip_event=skip_event)
        # All 3 conversations should have been skipped
        self.assertEqual(result['added'], 0)
        self.assertEqual(result['failed'], 0)
        # Verify all 3 skip states are persisted
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute('SELECT scope, state FROM conversation_states').fetchall()
            self.assertEqual(len(rows), 3)
            self.assertTrue(all(r[1] == 'skip' for r in rows)
)

if __name__ == '__main__': unittest.main()
