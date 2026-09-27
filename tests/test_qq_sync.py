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
        for kind in ('image','file','record','video','face','json','forward','at','reply'):
            item = raw(1, kind)
            self.assertIsNone(qq_to_message(item,self.conv,10))
            item['message'].append(dict(type='text',data=dict(text='hello')))
            self.assertIsNone(qq_to_message(item,self.conv,10))
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
            sync.assert_called_once_with()
    def test_api_pagination_parameters_and_error(self):
        client = QQClient()
        with patch.object(client,'_request',return_value={'messages':[]}) as request:
            client.get_history_page('private',20,3,'123')
            self.assertEqual(request.call_args.args[1]['message_seq'],'123')
            self.assertEqual(request.call_args.args[1]['reverseOrder'],'true')
        with patch.object(client,'_request',return_value={}):
            with self.assertRaises(QQClientError): client.get_history_page('group',20)
    def test_cq_string_and_array_literal(self):
        self.assertIsNone(qq_to_message(raw(1,message='hi[CQ:image,file=x]'),self.conv,10))
        self.assertEqual(qq_to_message(raw(1,message='&#91;CQ:test&#93;&amp;'),self.conv,10).content['text'],'[CQ:test]&')


if __name__ == '__main__': unittest.main()
