"""Unified querying and selected imports, with real run_turn/Memory lifecycle."""
from contextlib import closing, redirect_stdout
from dataclasses import asdict
from io import StringIO
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent.main import main, parse_tool_command, run_turn
from agent.messages import list_messages, query_messages, read_message
from agent.messages.importing import import_message
from agent.messages.models import Message
from agent.messages.sources import SOURCES
from agent.qq_sync import QQStore
from agent.tools import FileTools
from test_email_memory import ScriptClient, create


def message(id, *, date='2026-09-28T08:00:00+08:00', peer='20', imported=False):
    return Message(id, 'qq', date, imported, dict(text=' 完整中文\nEnglish\n末尾  ',
        sender=dict(user_id=30, nickname='小明'),
        conversation=dict(type='group', id=peer, name='测试群'), account_id='10', message_id=id))


class UnifiedMessageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base/'memory'
        self.root.mkdir()
        (self.root/'AGENT.md').write_text('Test protocol', encoding='utf-8')
        self.files = FileTools(self.root, lambda changes: {i:c.after for i,c in enumerate(changes)}, lambda *args:'yes')
        self.addCleanup(self.files.policy.close)
        self.store = QQStore(self.base/'qq/messages.sqlite3')
        self.addCleanup(patch.stopall)
        patch('agent.qq_sync.DB_PATH', self.store.path).start()
        patch('agent.email_index.INDEX_PATH', self.base/'email/index.json').start()
        self.save(message(1))

    def save(self, item):
        with closing(self.store.connect()) as db:
            db.execute('INSERT OR REPLACE INTO messages VALUES (?,?)', (str(item.id), json.dumps(asdict(item))))
            db.commit()

    def execute(self, client=None, id=1):
        with self.files.email_context(client or ScriptClient([]), emit=lambda _:None):
            return self.files.execute('import_message', dict(source='qq', id=id))

    def test_query_filters_order_summary_and_no_side_effects(self):
        self.save(message(2,date='2026-09-28T01:00:00+00:00',imported=True))
        self.save(message(3,date='2026-09-27T00:00:00+08:00',peer='21'))
        self.assertEqual([m.id for m in list_messages('qq')], [2,1,3])
        self.assertEqual([m.id for m in list_messages('qq',conversation='group:20',imported=False)], [1])
        self.assertEqual([m.id for m in list_messages('qq',time_from='2026-09-28T00:00:00Z',time_to='2026-09-28T00:00:00Z')], [1])
        result=query_messages('qq',limit=1)
        self.assertEqual(result['count'],1)
        self.assertEqual(result['messages'][0]['source'],'qq')
        self.assertTrue(result['messages'][0]['imported'])
        for key in ('id','time','conversation','sender','preview','imported'):
            self.assertIn(key,result['messages'][0])
        self.assertEqual(list_messages('email'),[])
        self.assertFalse(self.files.policy.changes)

    def test_query_rejects_invalid_filters(self):
        for filters in (dict(limit=0),dict(limit=True),dict(limit=201),dict(imported='false'),
                        dict(time_from='today'),dict(time_from='2026-09-28'),dict(conversation=3),
                        dict(time_from='2026-09-29T00:00:00Z',time_to='2026-09-28T00:00:00Z')):
            with self.subTest(filters=filters), self.assertRaises(ValueError): list_messages('qq',**filters)

    def test_unknown_time_and_source_id_ambiguity(self):
        self.save(message(2,date=None))
        self.assertEqual([m.id for m in list_messages('qq')],[1,2])
        self.assertEqual([m.id for m in list_messages('qq',time_from='2026-09-01T00:00:00Z')],[1])
        extra=Mock(list=Mock(return_value=[Message(1,'other',None,False,{})]))
        with patch.dict(SOURCES,{'other':lambda:extra}):
            with self.assertRaisesRegex(ValueError,'跨来源重复'):
                read_message(1)
            self.assertEqual(read_message(1,'qq')['source'],'qq')

    def test_sync_after_import_preserves_imported(self):
        from agent.qq_sync import update_qq
        from test_qq_sync import Client, raw
        result=update_qq(client=Client([raw(10)]),db_path=self.store.path)
        self.assertEqual(result['added'],1)
        selected=next(m for m in self.store.list() if m.id != 1)
        self.assertTrue(self.execute(id=selected.id)['imported'])
        self.assertEqual(update_qq(client=Client([raw(10)]),db_path=self.store.path)['added'],0)
        self.assertTrue(self.store.get(selected.id).imported)

    def test_read_full_qq_is_not_import(self):
        result = self.files.execute('read_message',dict(id=1,source='qq'))
        self.assertIn('来源：QQ',result['display'])
        self.assertTrue(result['display'].endswith(message(1).content['text']))
        self.assertFalse(self.store.get(1).imported)
        self.assertFalse(self.files.policy.changes)

    def test_formatter_and_real_run_turn_and_repeat(self):
        client=ScriptClient([])
        client.complete=Mock(wraps=client.complete)
        result=self.execute(client)
        self.assertTrue(result['imported'])
        system, transcript, tools=client.complete.call_args.args
        content=transcript[0]['content']
        for value in ('来源：QQ','2026-09-28T08:00:00+08:00','测试群','小明',message(1).content['text']):
            self.assertIn(value,content)
        self.assertNotIn('checkpoint',content)
        self.assertNotIn('account_id',content)
        self.assertIn('untrusted',system)
        self.assertNotIn('import_message',{s['name'] for s in tools})
        broken=Mock(complete=Mock(side_effect=AssertionError('must not run')))
        self.assertEqual(self.execute(broken)['status'],'already_imported')

    def test_pending_then_commit_marks_only_selected(self):
        self.save(message(2))
        result=self.execute(ScriptClient([[create('projects/a.md','candidate')]]))
        self.assertEqual(result['status'],'pending_review')
        self.assertFalse(self.store.get(1).imported)
        self.assertEqual(self.execute()['status'],'pending_review')
        committed=self.files.policy.request_commit()
        self.assertEqual(committed['status'],'committed')
        self.assertTrue(committed['message_imports'][0]['imported'])
        self.assertTrue(self.store.get(1).imported)
        self.assertFalse(self.store.get(2).imported)
        self.assertFalse((self.store.path.parent/'imports/1.lock').exists())

    def test_review_no_and_later_cancel_remains_false(self):
        self.files.policy.confirm_transaction=lambda *args:'no'
        result=self.execute(ScriptClient([[create('projects/a.md','candidate')],[('commit_memory_changes',{})]]))
        self.assertEqual(result['status'],'pending_review')
        run_turn(ScriptClient([]),self.files,[],'/cancel',emit=lambda _:None)
        self.assertFalse(self.store.get(1).imported)
        self.assertEqual(self.files.policy.completion_callbacks,{})
        self.assertTrue(self.execute()['imported'])

    def test_discard_inside_import_is_not_success(self):
        result=self.execute(ScriptClient([[create('projects/a.md','candidate')],[('discard_memory_changes',{})]]))
        self.assertEqual(result['status'],'discarded')
        self.assertFalse(self.store.get(1).imported)

    def test_commit_inside_import(self):
        result=self.execute(ScriptClient([[create('projects/a.md','candidate')],[('commit_memory_changes',{})]]))
        self.assertTrue(result['imported'])
        self.assertTrue((self.root/'projects/a.md').exists())

    def test_run_failure_and_memory_failure_do_not_mark(self):
        self.assertIn('error',self.execute(Mock(complete=Mock(side_effect=RuntimeError('model failed')))))
        self.assertFalse(self.store.get(1).imported)
        self.assertIn('error',self.execute(ScriptClient([[create('../escape.md','bad')]])))
        self.assertFalse(self.store.get(1).imported)
        self.assertFalse(self.files.processing_message)

    def test_exception_after_commit_still_not_imported(self):
        client=ScriptClient([[create('projects/a.md','candidate')],[('commit_memory_changes',{})]])
        original=client.complete
        def complete(*args):
            if not client.batches: raise RuntimeError('failed final response')
            return original(*args)
        client.complete=complete
        self.assertIn('error',self.execute(client))
        self.assertTrue((self.root/'projects/a.md').exists())
        self.assertFalse(self.store.get(1).imported)

    def test_commit_failure_retains_pending(self):
        self.execute(ScriptClient([[create('projects/a.md','candidate')]]))
        with patch.object(self.files.policy,'_commit_tree',side_effect=OSError('disk full')):
            result=self.files.execute('commit_memory_changes',{})
        self.assertIn('error',result)
        self.assertFalse(self.store.get(1).imported)
        self.files.policy.discard(explicit=True)
        self.assertFalse(self.store.get(1).imported)

    def test_imported_write_failure_reports_without_undoing_commit(self):
        self.execute(ScriptClient([[create('projects/a.md','candidate')]]))
        with patch.object(QQStore,'mark_imported',side_effect=OSError('disk full')):
            result=self.files.policy.request_commit()
        self.assertEqual(result['status'],'committed')
        self.assertIn('error',result['message_imports'][0])
        self.assertFalse(self.store.get(1).imported)
        self.assertFalse((self.store.path.parent/'imports/1.lock').exists())

    def test_self_review_rejection_and_close(self):
        self.files.policy.confirm_batch=lambda changes:{}
        result=self.execute(ScriptClient([[create('self/a.md','candidate')],[('commit_memory_changes',{})]]))
        self.assertEqual(result['status'],'pending_review')
        self.files.policy.close()
        self.assertFalse(self.store.get(1).imported)
        self.assertFalse((self.store.path.parent/'imports/1.lock').exists())

    def test_identity_change_before_commit_rejected(self):
        self.execute(ScriptClient([[create('projects/a.md','candidate')]]))
        changed=message(1); changed.content['text']='changed'; self.save(changed)
        result=self.files.policy.request_commit()
        self.assertIn('error',result['message_imports'][0])
        self.assertFalse(self.store.get(1).imported)

    def test_multiple_pending_imports_share_final_commit(self):
        self.save(message(2))
        for id in (1,2):
            self.assertEqual(self.execute(ScriptClient([[create(f'projects/{id}.md','candidate')]]),id=id)['status'],'pending_review')
        self.assertEqual(len(self.files.policy.request_commit()['message_imports']),2)
        self.assertTrue(all(m.imported for m in self.store.list()))

    def test_pending_lock_blocks_other_memory_session(self):
        self.execute(ScriptClient([[create('projects/a.md','candidate')]]))
        other=self.base/'other'; other.mkdir(); (other/'AGENT.md').write_text('Test')
        files=FileTools(other,lambda c:{},lambda *a:'yes')
        try:
            with self.assertRaisesRegex(ValueError,'正在导入'):
                import_message(1,ScriptClient([]),files,source='qq',emit=lambda _:None)
        finally: files.policy.close()

    def test_recursive_import_and_sync_rejected(self):
        for tool,args in (('import_message',dict(id=1,source='qq')),('update_qq',{})):
            self.assertIn('error',self.execute(ScriptClient([[(tool,args)]])))
            self.assertFalse(self.store.get(1).imported)

    def test_source_registry_extension_uses_same_import_coordinator(self):
        item=Message(99,'fake',None,False,{'text':'test'})
        backend=Mock(get=Mock(return_value=item),process=Mock(return_value={'status':'processed'}),
                     mark_imported=Mock(return_value={'imported':True}))
        from contextlib import nullcontext
        backend.lock=Mock(return_value=nullcontext())
        with patch.dict(SOURCES,{'fake':lambda:backend}):
            result=import_message(99,ScriptClient([]),self.files,source='fake',emit=lambda _:None)
        self.assertTrue(result['imported'])
        backend.process.assert_called_once()
        backend.mark_imported.assert_called_once_with(item)

    def test_cli_parsing_and_actual_chat(self):
        self.assertEqual(parse_tool_command('/import_message 1'),('import_message',{'id':1}))
        self.assertEqual(parse_tool_command('/read_message qq 1'),('read_message',{'source':'qq','id':1}))
        self.assertEqual(parse_tool_command('/list_messages qq {"imported":false,"limit":2}'),('list_messages',dict(source='qq',imported=False,limit=2)))
        for text in ('/import_message','/import_message qq -1','/read_message 0','/list_messages','/list_messages qq []'):
            with self.assertRaises(ValueError): parse_tool_command(text)
        client=ScriptClient([]); client.model='test'
        with patch('sys.argv',['agent.main']),patch('agent.main.FileTools',return_value=self.files),patch('agent.main.Client',return_value=client),patch('agent.main.load_config',return_value={'ANTHROPIC_AUTH_TOKEN':'test'}),patch('builtins.input',side_effect=['/list_messages qq','/read_message qq 1','/import_message qq 1','/exit']),redirect_stdout(StringIO()) as output:
            self.assertEqual(main(),0)
        self.assertIn('测试群',output.getvalue())
        self.assertIn('processed',output.getvalue())
        self.assertTrue(self.store.get(1).imported)

    def test_agent_tool_nested_transcript_is_valid(self):
        calls=[]
        class Client:
            def complete(inner,system,messages,tools):
                if 'user-selected message import task' in system:
                    self.assertIsInstance(messages[0]['content'],str)
                    calls.append('inner')
                    return dict(content=[dict(type='text',text='done')],stop_reason='end_turn')
                if not calls:
                    calls.append('outer')
                    return dict(content=[dict(type='tool_use',id='select',name='import_message',input=dict(source='qq',id=1))],stop_reason='tool_use')
                self.assertEqual(messages[-1]['content'][0]['tool_use_id'],'select')
                return dict(content=[dict(type='text',text='done')],stop_reason='end_turn')
        run_turn(Client(),self.files,[],'导入 QQ Message 1',emit=lambda _:None)
        self.assertEqual(calls,['outer','inner'])
        self.assertTrue(self.store.get(1).imported)


if __name__=='__main__': unittest.main()
