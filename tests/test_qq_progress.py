"""Progress is observational: compare persisted data/results with and without observers."""
from contextlib import closing, redirect_stdout
from functools import partial
from io import StringIO
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent.main import main, run_turn
from agent.qq_client import QQClientError
from agent.qq_progress import QQSyncProgress
from agent.qq_sync import QQStore, update_qq
from agent.tools import FileTools
from test_qq_sync import Client, raw

COUNTERS = ('scanned','text','added','duplicates','skipped','failed')


class MultipleChats(Client):
    def __init__(self, fail=False):
        super().__init__([raw(i) for i in range(1,8)] + [raw(8,'image')])
        self.fail = fail
    def list_group_chats(self): return [dict(group_id=99,group_name='讨论群')]
    def get_history_page(self,kind,peer,count,cursor):
        if self.fail and kind == 'group' and cursor is not None:
            raise QQClientError('page unavailable')
        return super().get_history_page(kind,peer,count,cursor)


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name)

    def snapshot(self,path):
        with closing(QQStore(path).connect()) as db:
            return {name:db.execute(f'SELECT * FROM {name} ORDER BY 1').fetchall()
                    for name in ('messages','checkpoints')}

    def test_no_callback_and_healthy_callback_are_identical(self):
        events=[]
        first=update_qq(client=MultipleChats(),db_path=self.base/'one.db',page_size=3)
        other=update_qq(client=MultipleChats(),db_path=self.base/'two.db',page_size=3,progress=events.append)
        self.assertEqual(first,other)
        self.assertEqual(self.snapshot(self.base/'one.db'),self.snapshot(self.base/'two.db'))
        self.assertNotIn('progress',other)
        self.assertEqual({e['total_conversations'] for e in events if e['event']=='start'},{2})
        self.assertEqual([e['completed_conversations'] for e in events if e['event']=='conversation_end'],[1,2])
        self.assertEqual(events[-1]['total_counts'],{k:first[k] for k in COUNTERS})
        self.assertEqual(events[-1]['completed_conversations'],2)
        pages=[e for e in events if e['event']=='page' and e['current_conversation']==1]
        self.assertGreater(len(pages),2)
        self.assertEqual([e['page'] for e in pages],list(range(1,len(pages)+1)))
        self.assertTrue(all(e['provisional'] for e in pages))
        self.assertEqual(pages[0]['committed_counts']['added'],0)
        self.assertGreater(pages[-1]['conversation_counts']['scanned'],pages[0]['conversation_counts']['scanned'])

    def test_duplicate_and_nontext_counts_match_finish(self):
        path=self.base/'db'
        update_qq(client=MultipleChats(),db_path=path,page_size=3)
        events=[]
        result=update_qq(client=MultipleChats(),db_path=path,page_size=3,progress=events.append)
        self.assertEqual(result['added'],0)
        self.assertGreater(result['duplicates'],0)
        self.assertGreater(result['skipped'],0)
        self.assertEqual(events[-1]['total_counts'],{k:result[k] for k in COUNTERS})

    def test_rollback_error_visible_and_next_conversation_continues(self):
        events=[]
        result=update_qq(client=MultipleChats(fail=True),db_path=self.base/'db',page_size=3,progress=events.append)
        failed=next(e for e in events if e['event']=='error')
        self.assertTrue(failed['rolled_back'])
        self.assertGreater(failed['conversation_counts']['added'],0)
        self.assertEqual(failed['conversation_counts']['failed'],1)
        self.assertEqual(failed['total_counts']['added'],0)
        self.assertEqual(failed['total_counts']['failed'],1)
        ends=[e for e in events if e['event']=='conversation_end']
        self.assertEqual([e['completed_conversations'] for e in ends],[1,2])
        self.assertTrue(any(e['event']=='page' and e['current_conversation']==2 for e in events))
        self.assertEqual(result['added'],7)
        self.assertEqual(result['failed'],1)
        self.assertEqual(events[-1]['total_counts'],{k:result[k] for k in COUNTERS})
        expected=update_qq(client=MultipleChats(fail=True),db_path=self.base/'plain',page_size=3)
        self.assertEqual(result,expected)
        self.assertEqual(self.snapshot(self.base/'db'),self.snapshot(self.base/'plain'))

    def test_observer_mutation_and_errors_cannot_change_sync(self):
        calls=[]
        def broken(event):
            calls.append(event['event'])
            event['total_counts']['added']=-999
            event['conversation_counts']['scanned']=-999
            if event['conversation']: event['conversation']['id']='wrong'
            raise RuntimeError('display failed')
        expected=update_qq(client=MultipleChats(),db_path=self.base/'plain',page_size=3)
        actual=update_qq(client=MultipleChats(),db_path=self.base/'broken',page_size=3,progress=broken)
        self.assertEqual(expected,actual)
        self.assertEqual(self.snapshot(self.base/'plain'),self.snapshot(self.base/'broken'))
        self.assertIn('page',calls)
        self.assertEqual(calls[-1],'finish')

    def test_message_failure_snapshots_and_checkpoint_unchanged(self):
        events=[]
        client=Client([raw(1,time='bad'),raw(2)])
        result=update_qq(client=client,db_path=self.base/'db',progress=events.append)
        errors=[e for e in events if e['event']=='message_error']
        self.assertEqual(len(errors),1)
        self.assertEqual(errors[0]['conversation_counts']['failed'],1)
        self.assertEqual(result['failed'],1)
        self.assertEqual(events[-1]['total_counts'],{k:result[k] for k in COUNTERS})
        self.assertEqual(self.snapshot(self.base/'db')['checkpoints'],[])

    def test_enumeration_failure_keeps_known_conversations_and_error(self):
        events=[]; client=MultipleChats()
        with patch.object(client,'list_group_chats',side_effect=QQClientError('groups unavailable')):
            result=update_qq(client=client,db_path=self.base/'db',progress=events.append)
        self.assertEqual(result['failed'],1)
        self.assertEqual(next(e for e in events if e['event']=='start')['total_conversations'],1)
        self.assertEqual(events[-1]['completed_conversations'],1)
        self.assertTrue(any(e['event']=='error' and e['conversation']=={'type':'group'} for e in events))

    def test_empty_and_login_failure_finish_without_fake_completed_chats(self):
        events=[]; client=Client([])
        with patch.object(client,'list_private_chats',return_value=[]):
            result=update_qq(client=client,db_path=self.base/'empty',progress=events.append)
        self.assertEqual((events[-1]['total_conversations'],events[-1]['completed_conversations']),(0,0))
        self.assertEqual(result['failed'],0)
        events=[]
        with patch.object(client,'get_login_info',side_effect=QQClientError('offline')):
            result=update_qq(client=client,db_path=self.base/'offline',progress=events.append)
        self.assertEqual([e['event'] for e in events],['connecting','error','finish'])
        self.assertIsNone(events[-1]['total_conversations'])
        self.assertEqual(events[-1]['total_counts']['failed'],result['failed'])


class RendererTests(unittest.TestCase):
    def state(self,event='page',**changes):
        state=dict(event=event,current_conversation=1,completed_conversations=0,total_conversations=2,
                   conversation=dict(type='group',id=123,name='群\n\x1b[2J'),page=3,
                   conversation_counts=dict.fromkeys(COUNTERS,1),total_counts=dict.fromkeys(COUNTERS,2),
                   provisional=True,rolled_back=False,error=None)
        state.update(changes)
        return state

    def test_interactive_refresh_wraps_and_preserves_error_lines(self):
        out=StringIO()
        def emit(text,**kwargs): print(text,file=out,**kwargs)
        render=QQSyncProgress(emit,interactive=True,width=60)
        render(self.state())
        render(self.state(page=4))
        self.assertIn('\r\x1b[',out.getvalue())
        self.assertIn('A\x1b[J',out.getvalue())
        self.assertNotIn('群\n\x1b[2J',out.getvalue())
        render(self.state('error',error='offline',rolled_back=True))
        self.assertEqual(render.lines,0)
        render(self.state('conversation_end',completed_conversations=1,provisional=False,rolled_back=True))
        render(self.state('finish',completed_conversations=2,conversation=None,provisional=False))
        self.assertIn('同步失败：offline',out.getvalue())
        self.assertIn('已回滚',out.getvalue().replace('\n',''))
        self.assertIn('2/2 100%',out.getvalue())
        self.assertTrue(out.getvalue().endswith('\n'))

    def test_plain_emit_throttles_only_pages_and_keeps_errors_finish(self):
        output=[]; now=[0.0]
        render=QQSyncProgress(output.append,clock=lambda:now[0])
        render(self.state('conversation_start'))
        render(self.state(page=1))
        render(self.state(page=2))
        self.assertEqual(len(output),2)
        now[0]=1.1; render(self.state(page=3))
        render(self.state('error',error='bad'))
        render(self.state('conversation_end',completed_conversations=1,provisional=False))
        render(self.state('finish',completed_conversations=2,conversation=None,provisional=False))
        text='\n'.join(output)
        self.assertIn('page=3',text)
        self.assertIn('同步失败：bad',text)
        self.assertIn('2/2 100%',text)
        self.assertNotIn('\x1b',text)
        self.assertNotIn('\r',text)
        self.assertIn('待提交',text)

    def test_output_failure_and_close_are_best_effort(self):
        def fail(*args,**kwargs): raise OSError('broken pipe')
        render=QQSyncProgress(fail,interactive=True)
        render(self.state())
        render(self.state('finish',completed_conversations=2))
        render.lines=2
        render.close()
        self.assertEqual(render.lines,0)

    def test_redirected_stdout_uses_plain_text(self):
        out=StringIO()
        with redirect_stdout(out):
            render=QQSyncProgress()
            self.assertFalse(render.interactive)
            render(self.state('finish',completed_conversations=2,conversation=None))
        self.assertNotIn('\r',out.getvalue())
        self.assertIn('2/2 100%',out.getvalue())


    def test_pre_marked_skip_shows_skipped_label(self):
        """Pre-marked skip conversations show as skipped with correct label."""
        import json, sqlite3
        from contextlib import closing
        from test_qq_sync import Client, raw
        from agent.qq_sync import QQStore
        tmp = tempfile.mkdtemp()
        path = Path(tmp) / 'qq.db'
        # First sync creates table
        update_qq(client=Client([raw(1)]), db_path=path, page_size=3)
        # Pre-mark as skip
        scope = json.dumps(['10', 'private', '20'])
        with closing(sqlite3.connect(path)) as db:
            db.execute('INSERT INTO conversation_states VALUES (?, ?)', (scope, 'skip'))
            db.commit()
        events = []
        update_qq(client=Client([raw(1)]), db_path=path, page_size=3, progress=events.append)
        skipped = [e for e in events if e['event'] == 'conversation_skipped']
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]['conversation']['type'], 'private')

    def test_user_skip_event_visible_in_progress(self):
        """User-initiated skip during sync shows conversation_skipped_user event."""
        import threading
        from test_qq_sync import Client, raw
        client = Client([raw(i) for i in range(20)])
        skip_event = threading.Event()
        events = []
        call_count = [0]
        original = client.get_history_page
        def intercept(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] >= 2:
                skip_event.set()
            return original(*args, **kwargs)
        client.get_history_page = intercept
        tmp = tempfile.mkdtemp()
        update_qq(client=client, db_path=Path(tmp) / 'qq.db', page_size=3,
                  skip_event=skip_event, progress=events.append)
        skipped = [e for e in events if e['event'] == 'conversation_skipped_user']
        self.assertEqual(len(skipped), 1)
        self.assertTrue(skipped[0]['rolled_back'])
        # No error events from the skip itself
        error_events = [e for e in events if e['event'] == 'error']
        self.assertEqual(len(error_events), 0)



class RuntimeProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name); root=self.base/'memory'; root.mkdir()
        (root/'AGENT.md').write_text('Test',encoding='utf-8')
        self.files=FileTools(root,lambda changes:{},lambda *args:'no')
        self.addCleanup(self.files.policy.close)

    def core(self):
        return patch('agent.qq_sync.update_qq',wraps=partial(update_qq,client=Client([raw(1)]),db_path=self.base/'db'))

    def test_direct_command_and_agent_share_core_and_runtime_emit(self):
        client=type('Client',(),{'model':'test'})()
        out=StringIO()
        with self.core() as core, patch('sys.argv',['agent.main']),patch('agent.main.FileTools',return_value=self.files),patch('agent.main.Client',return_value=client),patch('agent.main.load_config',return_value={'ANTHROPIC_AUTH_TOKEN':'test'}),patch('builtins.input',side_effect=['/update_qq','/exit']),redirect_stdout(out):
            self.assertEqual(main(),0)
            core.assert_called_once()
            self.assertTrue(callable(core.call_args.kwargs['progress']))
        self.assertIn('1/1 100%',out.getvalue())
        self.assertIn('QQ 同步完成',out.getvalue())
        self.assertIn('当前：私聊 friend 20',out.getvalue())
        class Agent:
            def __init__(self): self.calls=0
            def complete(inner,system,messages,tools):
                inner.calls+=1
                if inner.calls==1:
                    return dict(content=[dict(type='tool_use',id='sync',name='update_qq',input={})],stop_reason='tool_use')
                return dict(content=[dict(type='text',text='done')],stop_reason='end_turn')
        output=[]
        with self.core() as core:
            run_turn(Agent(),self.files,[],'同步 QQ',emit=output.append)
            core.assert_called_once()
            self.assertIs(core.call_args.kwargs['progress'].emit.__self__,output)
        self.assertTrue(any('1/1 100%' in line for line in output))
        self.assertTrue(any('QQ 同步完成' in line for line in output))
        self.assertIsNone(self.files._message_context)




class ConsoleInputTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows console polling")
    def test_polling_enter_and_no_read_without_available_input(self):
        from agent.qq_progress import ConsoleSkipEvent
        event = ConsoleSkipEvent()
        with patch.object(event.console, 'kbhit', side_effect=[True, False]), \
             patch.object(event.console, 'getwch', return_value='\r') as read:
            self.assertTrue(event.is_set())
            read.assert_called_once()
        event.clear()
        with patch.object(event.console, 'kbhit', return_value=False), \
             patch.object(event.console, 'getwch', side_effect=AssertionError('blocking read')):
            self.assertFalse(event.is_set())

    @unittest.skipUnless(os.name == "nt", "Windows console polling")
    def test_nonempty_line_is_not_skip_and_extended_keys_are_ignored(self):
        from agent.qq_progress import ConsoleSkipEvent
        event = ConsoleSkipEvent()
        with patch.object(event.console, 'kbhit', side_effect=[True]*5+[False]), \
             patch.object(event.console, 'getwch', side_effect=['\xe0', 'H', 'x', 'y', '\r']):
            self.assertFalse(event.is_set())

    def test_redirected_input_never_starts_reader(self):
        with patch('sys.stdin.isatty', return_value=False), \
             patch('agent.qq_sync.update_qq', return_value={}) as sync, \
             patch('agent.qq_progress.ConsoleSkipEvent', side_effect=AssertionError('input reader')):
            FileTools.update_qq(object(), interactive=True)
        self.assertIsNone(sync.call_args.kwargs['skip_event'])


if __name__ == '__main__': unittest.main()
