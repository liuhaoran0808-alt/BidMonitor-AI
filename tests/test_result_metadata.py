"""统计/时间回归：只使用临时数据库及提取的纯 API 函数。"""
import ast
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

PROJECT = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get('BIDMONITOR_TEST_ROOT', PROJECT))

spec = importlib.util.spec_from_file_location('_metadata_test_storage', ROOT / 'src/database/storage.py')
storage_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = storage_module
spec.loader.exec_module(storage_module)
BidInfo, Storage = storage_module.BidInfo, storage_module.Storage


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 11, 9, 15, 22, tzinfo=timezone(timedelta(hours=8)))
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


def api_namespace(records):
    tree = ast.parse((ROOT / 'server/app.py').read_text(encoding='utf-8-sig'))
    wanted = {'_local_created_at', '_result_item', 'get_status', 'get_results'}
    functions = [copy.deepcopy(n) for n in tree.body
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in wanted]
    assert len(functions) == 4
    for function in functions:
        function.decorator_list = []
    state = SimpleNamespace(
        storage=SimpleNamespace(get_all=lambda: list(records)), is_running=False,
        last_run_time=None, next_run_time=None, today_rounds=0, config={},
        progress_current=0, progress_total=0, progress_site='', current_task_running=False)
    ns = {'datetime': FixedDateTime, 'timezone': timezone, 'timedelta': timedelta, 'app_state': state}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(ROOT / 'server/app.py'), 'exec'), ns)
    return ns


def bid(name='企业样例', published='', created='2026-11-09 07:18:24'):
    return BidInfo(name, 'https://example.org/' + name, published, '本地测试', created_at=created)


def run_coroutine(coroutine):
    """这两个接口没有 await；执行函数体，无需启动 Windows IO 事件循环。"""
    try:
        coroutine.send(None)
    except StopIteration as completed:
        return completed.value
    else:
        coroutine.close()
        raise AssertionError('该隔离测试不应执行外部异步操作')


class ResultMetadataTests(unittest.TestCase):
    def test_company_counts_by_insertion_not_missing_publication(self):
        ns = api_namespace([bid()])
        self.assertEqual(run_coroutine(ns['get_status']())['today_new'], 1)

    def test_old_record_published_today_is_not_new(self):
        ns = api_namespace([bid(published='2026-11-09', created='2026-11-08 15:59:59')])
        self.assertEqual(run_coroutine(ns['get_status']())['today_new'], 0)

    def test_utc_plus_eight_midnight_boundary(self):
        ns = api_namespace([bid('昨天', created='2026-11-08 15:59:59'),
                            bid('今天', created='2026-11-08 16:00:00')])
        self.assertEqual(run_coroutine(ns['get_status']())['today_new'], 1)

    def test_backfilled_old_publication_counts_when_inserted_today(self):
        ns = api_namespace([bid(published='2020-01-01')])
        self.assertEqual(run_coroutine(ns['get_status']())['today_new'], 1)

    def test_missing_or_invalid_timestamp_is_not_fabricated(self):
        for value in ('', None, 'invalid'):
            ns = api_namespace([bid(created=value, published='2026-11-09')])
            self.assertEqual(run_coroutine(ns['get_status']())['today_new'], 0)
            self.assertIsNone(ns['_result_item'](bid(created=value))['collected_at'])

    def test_company_result_preserves_unknown_publication_and_local_collection(self):
        ns = api_namespace([bid()])
        item = run_coroutine(ns['get_results']())['items'][0]
        self.assertIsNone(item['pub_date'])
        self.assertEqual(item['collected_at'], '2026-11-09 15:18:24')

    def test_announcement_publication_is_preserved(self):
        ns = api_namespace([bid(published='2026-10-01')])
        self.assertEqual(run_coroutine(ns['get_results']())['items'][0]['pub_date'], '2026-10-01')

    def test_results_sort_by_collection_and_keep_pagination(self):
        records = [bid('旧公告', published='2026-12-01', created='2026-11-08 03:00:00'),
                   bid('新企业', created='2026-11-09 07:18:24')]
        ns = api_namespace(records)
        result = run_coroutine(ns['get_results'](limit=1, offset=0))
        self.assertEqual(result['total'], 2)
        self.assertEqual(result['items'][0]['title'], '新企业')
        self.assertEqual(run_coroutine(ns['get_results'](limit=1, offset=1))['items'][0]['title'], '旧公告')

    def test_already_aware_time_is_not_shifted_twice(self):
        ns = api_namespace([])
        for value in ('2026-11-09T07:18:24Z', '2026-11-09T15:18:24+08:00'):
            self.assertEqual(ns['_result_item'](bid(created=value))['collected_at'], '2026-11-09 15:18:24')

    def test_storage_reads_created_at_in_all_existing_read_methods(self):
        temporary_root = Path(os.environ.get('BIDMONITOR_TEST_TEMP', tempfile.gettempdir())).resolve()
        with tempfile.TemporaryDirectory(prefix='bidmonitor-metadata-', dir=str(temporary_root)) as temporary:
            self.assertEqual(Path(temporary).resolve().parent, temporary_root)
            store = Storage(str(Path(temporary) / 'test.db'))
            try:
                self.assertTrue(store.save(bid()))
                for records in (store.get_all(), store.get_unnotified(), store.get_recent()):
                    self.assertEqual(len(records), 1)
                    self.assertTrue(records[0].created_at)
                original = store.get_all()[0].created_at
                self.assertFalse(store.save(bid()))
                self.assertEqual(store.count_all(), 1)
                self.assertEqual(store.get_all()[0].created_at, original)
            finally:
                store.close()

    @unittest.skipUnless(shutil.which('node'), 'Node required for frontend regression')
    def test_frontend_script_syntax_and_correct_date_labels(self):
        html = str(ROOT / 'server/static/index.html')
        javascript = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(process.argv[1], 'utf8');
for (const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)) {
    if (match[1].trim()) new vm.Script(match[1]);
}
const helper = html.match(/function resultTimeLabel\(item\) \{[\s\S]*?\n        \}/);
assert(helper);
const context = vm.createContext({});
vm.runInContext(helper[0], context);
assert.strictEqual(context.resultTimeLabel({pub_date:'2026-11-01',collected_at:'2026-11-09 15:18:24'}), '添加：2026-11-09 15:18:24');
assert.strictEqual(context.resultTimeLabel({pub_date:null,collected_at:'2026-11-09 15:18:24'}), '添加：2026-11-09 15:18:24');
assert.strictEqual(context.resultTimeLabel({pub_date:null,collected_at:null}), '添加时间未知');
assert(html.includes('escapeHtml(resultTimeLabel(i))'));
'''
        result = subprocess.run([shutil.which('node'), '-e', javascript, html],
                                capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
