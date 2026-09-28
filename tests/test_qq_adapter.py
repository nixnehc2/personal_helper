"""Readable segments, minimum CQ compatibility and unchanged sync semantics."""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent.messages.qq_adapter import qq_to_message
from agent.qq_sync import QQStore, update_qq
from agent.qq_client import QQClientError
from test_qq_sync import Client, raw


def seg(kind, **data):
    return dict(type=kind, data=data)


class AdapterTests(unittest.TestCase):
    def convert(self, segments):
        return qq_to_message(raw(1, message=segments), dict(type='private', id=20, name='friend'), 10)

    def test_readable_segments_and_original_order(self):
        text = seg('text', text=' 中文\nEnglish  ')
        at = seg('at', qq='123')
        reply = seg('reply', id='456')
        image = seg('image', file='synthetic.png')
        cases = [
            ([text], ' 中文\nEnglish  '),
            ([text, seg('text', text='尾\n ')], ' 中文\nEnglish  尾\n '),
            ([at, text], '@123 中文\nEnglish  '),
            ([text, at], ' 中文\nEnglish  @123'),
            ([reply, text], '[回复:456] 中文\nEnglish  '),
            ([text, reply], ' 中文\nEnglish  [回复:456]'),
            ([image, text], ' 中文\nEnglish  '),
            ([text, image], ' 中文\nEnglish  '),
            ([at, text, image], '@123 中文\nEnglish  '),
            ([at, image, text], '@123 中文\nEnglish  '),
            ([reply, text, image], '[回复:456] 中文\nEnglish  '),
            ([at], '@123'), ([seg('at', qq=123)], '@123'),
            ([seg('at', qq='all')], '@全体成员'),
            ([reply], '[回复:456]'), ([seg('reply', id=-456)], '[回复:-456]'),
            ([seg('text', text='\n  ')], '\n  '),
        ]
        for kind in ('video', 'file', 'record', 'face', 'mface', 'forward', 'json', 'share', 'future_type'):
            ignored = seg(kind, data='not parsed', title='not extracted')
            cases.append(([ignored, text], ' 中文\nEnglish  '))
            cases.append(([text, ignored, seg('text', text='尾')], ' 中文\nEnglish  尾'))
        for segments, expected in cases:
            with self.subTest(segments=segments):
                message = self.convert(segments)
                self.assertEqual(message.content['text'], expected)
                self.assertEqual(message.content['segments'], segments)

    def test_only_unsupported_or_empty_content_skips(self):
        for kind in ('image', 'video', 'file', 'record', 'face', 'mface', 'forward', 'json', 'share', 'unknown'):
            with self.subTest(kind=kind):
                self.assertIsNone(self.convert([seg(kind, data='unparsed')]))
        for segments in ([], '', [seg('text', text='')], [seg('image'), seg('json')]):
            self.assertIsNone(self.convert(segments))

    def test_malformed_data_raises_instead_of_skipping(self):
        for segments in (None, {}, 1, [None], ['bad'], [{}],
                         [dict(type='text')], [dict(type='image', data=[])],
                         [dict(type=2, data={})], [seg('text', text=123)],
                         [seg('text')], [seg('at')], [seg('at', qq=True)],
                         [seg('reply', id='')], [seg('reply', id={})]):
            with self.subTest(segments=segments), self.assertRaises(ValueError):
                self.convert(segments)
        with self.assertRaises(ValueError):
            qq_to_message([], dict(type='private', id=20), 10)

    def test_segments_snapshot_does_not_alias_input_and_id_unchanged(self):
        segments = [seg('text', text='hello'), seg('json', data='{"title":"raw"}'),
                    seg('image', metadata={'file': 'synthetic'})]
        original = deepcopy(segments)
        message = self.convert(segments)
        self.assertEqual(segments, original)
        message.content['segments'][2]['data']['metadata']['file'] = 'changed'
        self.assertEqual(segments, original)
        self.assertEqual(message.id, self.convert([seg('text', text='other text')]).id)
        self.assertEqual(set(vars(message)), {'id', 'source', 'time', 'imported', 'content'})

    def test_minimal_cq_preserves_surrounding_text_and_escaping(self):
        cases = [
            (' 中文\nEnglish  ', ' 中文\nEnglish  '),
            ('[CQ:at,qq=123] 你看一下', '@123 你看一下'),
            ('text[CQ:at,qq=all]', 'text@全体成员'),
            ('[CQ:reply,id=456]好的', '[回复:456]好的'),
            ('text[CQ:reply,id=-456]', 'text[回复:-456]'),
            ('看看这个[CQ:image,file=xxx]', '看看这个'),
            ('[CQ:at,qq=123][CQ:image,file=x] 后面的文字', '@123 后面的文字'),
            ('[CQ:reply,id=456]好的[CQ:video,file=x]', '[回复:456]好的'),
            ('&#91;CQ:at,qq=123&#93;&amp;', '[CQ:at,qq=123]&'),
            ('&amp;#91;', '&#91;'),
            (' [CQ:image,file=x]\n[CQ:json,data=ignored] ', ' \n '),
        ]
        for kind in ('image', 'file', 'record', 'video', 'face', 'mface', 'forward', 'json', 'share', 'unknown'):
            code = f'[CQ:{kind},data=ignored]'
            cases.append((f'前{code}后', '前后'))
            self.assertIsNone(self.convert(code))
        for source, expected in cases:
            with self.subTest(source=source):
                message = self.convert(source)
                self.assertEqual(message.content['text'], expected)
                self.assertEqual(message.content['segments'], source)
        literal = '&#91;CQ:image,file=x&#93;&amp;'
        self.assertEqual(self.convert([seg('text', text=literal)]).content['text'], literal)
        for source in ('[CQ:at]', '[CQ:reply,id=]'):
            with self.assertRaises(ValueError):
                self.convert(source)


class MixedSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'messages.sqlite3'
        text = seg('text', text='可读文字')
        self.batches = [[text], [seg('at', qq=123), text], [seg('reply', id=456), text],
                        [seg('image'), text], [seg('json', data='not-json'), text],
                        [seg('image')], [seg('video')], [seg('json', data='ignored')]]
        self.client = Client([raw(i, message=segments) for i, segments in enumerate(self.batches, 1)])

    def sync(self, page_size=3):
        return update_qq(client=self.client, db_path=self.path, page_size=page_size)

    def checkpoint(self):
        with closing(QQStore(self.path).connect()) as db:
            return db.execute('SELECT message_id FROM checkpoints').fetchall()

    def test_counts_checkpoint_duplicates_incremental_and_pagination(self):
        result = self.sync()
        self.assertEqual(tuple(result[k] for k in ('scanned', 'text', 'added', 'skipped', 'failed')), (8, 5, 5, 3, 0))
        self.assertGreater(len(self.client.calls), 1)
        self.assertEqual(self.checkpoint(), [('8',)])
        self.assertIn('可读消息：5', result['display'])
        self.assertIn('无文字内容跳过：3', result['display'])
        again = self.sync(100)
        self.assertEqual((again['added'], again['duplicates'], again['failed']), (0, 5, 0))
        self.client.messages.extend([raw(9, message=[seg('at', qq='all'), seg('text', text=' next'), seg('image')]),
                                     raw(10, message=[seg('share', title='not extracted')])])
        self.assertEqual(self.sync()['added'], 1)
        self.assertEqual(self.checkpoint(), [('10',)])
        self.assertEqual(len(QQStore(self.path).list()), 6)

    def test_mixed_page_failure_rolls_back_and_retries(self):
        original = self.client.get_history_page
        def history(kind, peer, count, cursor):
            if cursor is not None:
                raise QQClientError('offline')
            return original(kind, peer, count, cursor)
        # First page contains readable content, then the next page fails.
        with patch.object(self.client, 'get_history_page', side_effect=history):
            self.assertEqual(self.sync(5)['failed'], 1)
        self.assertEqual(QQStore(self.path).list(), [])
        self.assertEqual(self.checkpoint(), [])
        self.assertEqual(self.sync()['added'], 5)

    def test_bad_segment_does_not_advance_checkpoint(self):
        self.client.messages[0]['message'] = [seg('at')]
        self.assertEqual(self.sync()['failed'], 1)
        self.assertEqual(self.checkpoint(), [])
        self.client.messages[0]['message'] = [seg('at', qq='all')]
        self.assertEqual(self.sync()['added'], 1)
        self.assertEqual(self.checkpoint(), [('8',)])

    def test_read_list_search_and_import_loader_use_extracted_body(self):
        from agent.messages import list_messages, read_message, search_messages
        from agent.messages.importing import load_message_for_import
        self.sync()
        with patch('agent.qq_sync.DB_PATH', self.path):
            items = list_messages('qq')
            self.assertEqual(len(items), 5)
            target = next(m for m in items if m.content['message_id'] == 3)
            self.assertIn('[回复:456]可读文字', read_message(target.id, 'qq')['display'])
            self.assertEqual(search_messages('[回复:456]', 'qq')['count'], 1)
            _, _, loaded = load_message_for_import(target.id, 'qq')
            self.assertEqual(loaded['external_message']['content']['text'], '[回复:456]可读文字')
            self.assertEqual(loaded['external_message']['content']['segments'], self.batches[2])
            self.assertFalse(QQStore(self.path).get(target.id).imported)
