"""Offline regressions for the 2026-11-10 audit. No production DB or source-site requests."""
import asyncio
import copy
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

ROOT=Path(os.environ.get('BIDMONITOR_TEST_ROOT',Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(ROOT/'src'))
import public_bidding as public
import competitor
from database.storage import Storage, BidInfo
import database.storage as storage_module
from crawler.custom import CustomCrawler
from crawler.selenium_crawler import SeleniumCrawler
from bs4 import BeautifulSoup
from test_public_bidding import fixture, opening, A, URL, PDF


class DataReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=os.environ.get('BIDMONITOR_TEST_TEMP'),prefix='reliability-test-')
        self.addCleanup(self.temp.cleanup)
        self.directory=Path(self.temp.name)
        self.store=competitor.Records(str(self.directory/'test.db'))

    def test_decimal_unit_and_thousands_do_not_truncate(self):
        for token, expected in [('123456.78元','123456.78'),('1,234,567.89元','1,234,567.89'),('0.00元','0.00'),('123456.78','123456.78')]:
            with self.subTest(token=token):
                rows=public.parse_opening_text(opening().replace('8671000.00',token),A,PDF)
                self.assertEqual(rows[0]['fields']['投标报价'],expected)

    def test_invalid_numeric_tokens_are_rejected_not_truncated(self):
        for token in ['123.45.67','12,34.56','123.45e3','123.45万元','123.45%','-123.45','NaN','123.45abc']:
            with self.subTest(token=token):
                self.assertEqual(public.parse_opening_text(opening().replace('8671000.00',token),A,PDF),[])

    def test_multiline_decimal_keeps_precision(self):
        text=opening().replace('8671000.00','\n123456.78元\n')
        self.assertEqual(public.parse_opening_text(text,A,PDF)[0]['fields']['投标报价'],'123456.78')

    def test_generic_crawlers_do_not_invent_publication_date(self):
        crawler=SimpleNamespace(url='https://example.invalid/',name='offline',logger=Mock(),parse_html=lambda h:BeautifulSoup(h,'lxml'))
        for cls in (CustomCrawler,SeleniumCrawler):
            row=cls.parse(crawler,'<a href="/old">2018年测试公告</a><time>2018-01-01</time>')[0]
            self.assertEqual(row.publish_date,'')

    def test_weak_amount_change_is_retained_once_as_review(self):
        a=public.parse_trade_page(fixture(),A,URL)[1][0]
        b=public.parse_trade_page(fixture().replace('96.00','97.00'),A,URL)[1][0]
        self.assertTrue(self.store.save(A,'ggzy',a))
        for _ in range(2): self.assertFalse(self.store.save(A,'ggzy',b))
        result=self.store.list()[0]
        self.assertEqual(self.store.counts(),{'total':1,'today_new':1,'review_count':1})
        self.assertEqual(result['fields']['中标（成交）金额（万元）'],'96.00')
        self.assertEqual(result['observations'][0]['fields']['中标（成交）金额（万元）'],'97.00')

    def test_strong_lot_identity_updates_and_retains_old_amount(self):
        a=public.parse_trade_page(fixture(),A,URL)[1][0]
        a['fields'].update({'项目编号':'TEST-001','标段编号':'LOT-1'})
        self.store.save(A,'ggzy',a)
        before=self.store.list()[0]['collected_at']
        b=copy.deepcopy(a);b['fields']['中标（成交）金额（万元）']='97.00'
        self.assertFalse(self.store.save(A,'ggzy',b))
        row=self.store.list()[0]
        self.assertEqual(row['fields']['中标（成交）金额（万元）'],'97.00')
        self.assertEqual(row['observations'][0]['reason'],'superseded')
        self.assertEqual(row['observations'][0]['fields']['中标（成交）金额（万元）'],'96.00')
        self.assertEqual(row['collected_at'],before)
        self.assertEqual(self.store.counts()['review_count'],0)

    def test_different_lots_with_same_title_are_not_merged(self):
        for lot in ('LOT-1','LOT-2'):
            a=public.parse_trade_page(fixture(),A,URL)[1][0]
            a['fields'].update({'项目编号':'TEST-001','标段编号':lot})
            self.assertTrue(self.store.save(A,'ggzy',a))
        self.assertEqual(self.store.counts()['total'],2)

    def test_legacy_amount_based_key_is_not_duplicated_or_deleted(self):
        a=public.parse_trade_page(fixture(),A,URL)[1][0]
        with self.store.connect() as con:
            con.execute('INSERT INTO competitor_records(record_key,company,platform,kind,title,source_url,fields) VALUES(?,?,?,?,?,?,?)',
                        ('legacy-key',A,'ggzy','tender',a['title'],URL,json.dumps(a['fields'])))
        self.assertFalse(self.store.save(A,'ggzy',a))
        self.assertEqual(self.store.list()[0]['record_key'],'legacy-key')
        b=copy.deepcopy(a);b['fields']['中标（成交）金额（万元）']='98.00'
        self.assertFalse(self.store.save(A,'ggzy',b))
        self.assertEqual(self.store.counts()['total'],1)
        self.assertEqual(self.store.counts()['review_count'],1)

    def test_ambiguous_legacy_duplicates_remain_with_review_not_auto_deleted(self):
        a=public.parse_trade_page(fixture(),A,URL)[1][0]
        with self.store.connect() as con:
            for key in ('old-1','old-2'):
                con.execute('INSERT INTO competitor_records(record_key,company,platform,kind,title,source_url,fields) VALUES(?,?,?,?,?,?,?)',
                            (key,A,'ggzy','tender',a['title'],URL,json.dumps(a['fields'])))
        self.assertFalse(self.store.save(A,'ggzy',a))
        self.assertEqual(self.store.counts()['total'],2)
        self.assertEqual(self.store.counts()['review_count'],2)

    def test_default_path_does_not_depend_on_cwd(self):
        # Fake project root, never resolve or write the real default business DB.
        source=self.directory/'src/database/storage.py';source.parent.mkdir(parents=True);source.touch()
        env=dict(os.environ);env.pop('BIDMONITOR_DB_PATH',None)
        with patch.dict(os.environ,env,clear=True), patch.object(storage_module,'__file__',str(source)):
            expected=self.directory/'server/data/bids.db'
            self.assertEqual(storage_module.default_db_path(),str(expected.resolve()))
            old=Path.cwd()
            try:
                os.chdir(self.directory)
                self.assertEqual(storage_module.default_db_path(),str(expected.resolve()))
            finally:os.chdir(old)

    def test_two_legacy_databases_require_explicit_selection(self):
        source=self.directory/'src/database/storage.py';source.parent.mkdir(parents=True);source.touch()
        for rel in ('server/data/bids.db','data/bids.db'):
            f=self.directory/rel;f.parent.mkdir(parents=True);f.touch()
        env=dict(os.environ);env.pop('BIDMONITOR_DB_PATH',None)
        with patch.dict(os.environ,env,clear=True),patch.object(storage_module,'__file__',str(source)):
            with self.assertRaises(RuntimeError):storage_module.default_db_path()
            with patch.dict(os.environ,{'BIDMONITOR_DB_PATH':str(self.directory/'data/bids.db')}):
                self.assertEqual(storage_module.default_db_path(),str((self.directory/'data/bids.db').resolve()))

    def test_storage_clear_scopes_and_atomic_rollback(self):
        storage=Storage(self.store.path);self.addCleanup(storage.close)
        storage.save(BidInfo('offline','https://example.invalid/1','','test'))
        a=public.parse_trade_page(fixture(),A,URL)[1][0];self.store.save(A,'ggzy',a)
        b=copy.deepcopy(a);b['fields']['中标（成交）金额（万元）']='97.00';self.store.save(A,'ggzy',b)
        storage.clear_all('bids');self.assertEqual(storage.count_all(),0);self.assertEqual(self.store.counts()['total'],1)
        storage.save(BidInfo('offline','https://example.invalid/1','','test'))
        with self.store.connect() as con:
            con.execute("CREATE TRIGGER block_clear BEFORE DELETE ON competitor_records BEGIN SELECT RAISE(ABORT,'test-only-error'); END")
        with self.assertRaises(sqlite3.IntegrityError):storage.clear_all('all')
        self.assertEqual(storage.count_all(),1);self.assertEqual(self.store.counts()['review_count'],1)
        with self.store.connect() as con:con.execute('DROP TRIGGER block_clear')
        storage.clear_all('competitors');self.assertEqual(storage.count_all(),1);self.assertEqual(self.store.counts()['total'],0)
        self.assertEqual(self.store.counts()['review_count'],0)
        storage.clear_all('all');self.assertEqual(storage.count_all(),0)

    def test_tls_verification_is_enabled(self):
        crawler=CustomCrawler({'max_retries':1,'request_delay':0},'offline','https://example.invalid/')
        response=Mock();response.text='offline';response.apparent_encoding='utf-8'
        with patch.object(crawler.session,'get',return_value=response) as call,patch('crawler.base.time.sleep'):
            self.assertEqual(crawler.fetch(crawler.url),'offline')
            self.assertIs(call.call_args.kwargs['verify'],True)
        source=(ROOT/'src/crawler/selenium_crawler.py').read_text(encoding='utf-8')
        self.assertNotIn('--ignore-certificate-errors',source)
        self.assertNotIn('--ignore-ssl-errors',source)

    def test_pdf_dependency_in_both_install_routes(self):
        for name in ('requirements.txt','server/requirements.txt'):
            self.assertTrue(any(x.strip().startswith('pypdf') for x in (ROOT/name).read_text(encoding='utf-8-sig').splitlines()))

    def test_partial_pages_survive_verification_and_site_errors(self):
        from selenium.common.exceptions import TimeoutException, WebDriverException
        for error,expected in [(competitor.ManualRequired('test'),'verification_required'),
                               (competitor.SiteAccessError(521),'site_error'),
                               (TimeoutException('test'),'partial'),(WebDriverException('test'),'partial')]:
            with self.subTest(error=type(error).__name__):
                class Element:
                    def __init__(self,text='',action=None):self.text=text;self.action=action
                    def is_displayed(self):return True
                    def click(self):
                        if self.action:self.action()
                    def get_attribute(self,k):return ''
                class Driver:
                    phase='search';current_window_handle='owned';window_handles=['owned'];current_url=''
                    page_source=fixture()
                    switch_to=SimpleNamespace(window=lambda h:None)
                    def get(self,url):self.current_url=url;self.phase='search'
                    def detail(self):self.current_url=URL;self.phase='detail'
                    def block(self):self.phase='blocked'
                    def find_elements(self,by,selector):
                        if selector.endswith(', .title'):return [Element(A,self.detail)]
                        if selector=='.ivu-table-body tbody tr':return [Element('first')]
                        if selector=='.ivu-page-next':return [Element('next',self.block)]
                        return []
                driver=Driver()
                def access(d):
                    if d.phase=='blocked':raise error
                service=competitor.CompetitorService(str(self.directory/(type(error).__name__+'.db')))
                @contextmanager
                def session(platform):yield driver
                service.sessions.use=session;service.reserve(A,['ggzy'])
                with patch.object(competitor,'_check_access',access):service.run()
                entry=service.snapshot()['entries'][0]
                self.assertEqual(entry['status'],expected)
                self.assertEqual(entry['result_count'],2)
                self.assertEqual(service.records.counts()['total'],2)


class WebReliabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(dir=os.environ.get('BIDMONITOR_TEST_TEMP'),prefix='web-reliability-test-')
        cls.directory=Path(cls.temp.name)
        sys.path.insert(0,str(ROOT/'server'))
        with patch.dict(os.environ,{'BIDMONITOR_DB_PATH':str(cls.directory/'bootstrap.db'),'BIDMONITOR_USERNAME':'offline-user','BIDMONITOR_PASSWORD':'offline-secret'}):
            spec=importlib.util.spec_from_file_location('_reliability_app',ROOT/'server/app.py')
            cls.app=importlib.util.module_from_spec(spec);sys.modules[spec.name]=cls.app;spec.loader.exec_module(cls.app)
        cls.app.app_state.storage.close()

    @classmethod
    def tearDownClass(cls):
        cls.app.app_state.storage.close();cls.temp.cleanup()

    def setUp(self):
        self.app.app_state.storage.close()
        with patch.dict(os.environ,{'BIDMONITOR_DB_PATH':str(self.directory/(self._testMethodName+'.db'))}):
            self.app.app_state=self.app.AppState()
        self.app.competitor_service=competitor.CompetitorService(self.app.app_state.storage.db_path)
        self.app.CONFIG_FILE=str(self.directory/(self._testMethodName+'.json'))

    def run_async(self,fn):return asyncio.run(fn())

    def test_config_failure_keeps_memory_and_disk(self):
        app=self.app;app.app_state.config={'enabled_sites':['old'],'ai_config':{'api_key':'test-secret'}}
        app.save_config(app.app_state.config)
        before=Path(app.CONFIG_FILE).read_bytes()
        async def scenario():
            with patch.object(app.os,'replace',side_effect=OSError('test-only')):
                with self.assertRaises(app.HTTPException) as error:await app.update_sites(['new'])
                self.assertEqual(error.exception.status_code,500)
            self.assertEqual(app.app_state.config['enabled_sites'],['old'])
            self.assertEqual(Path(app.CONFIG_FILE).read_bytes(),before)
            self.assertEqual(list(self.directory.glob('.config-*.tmp')),[])
        self.run_async(scenario)

    def test_competitor_config_failure_releases_reservation(self):
        app=self.app
        async def scenario():
            with patch.object(app.os,'replace',side_effect=OSError('test-only')):
                with self.assertRaises(app.HTTPException):
                    await app.competitor_query(app.CompetitorQueryRequest(company_text=A,platforms=['ggzy']),app.BackgroundTasks())
            self.assertFalse(app.competitor_service.snapshot()['running'])
            self.assertTrue(app.competitor_service.reserve(A,['ggzy'])['running'])
        self.run_async(scenario)

    def test_secrets_masked_and_preserved_through_read_edit_save(self):
        app=self.app
        app.commit_config({'ai_config':{'api_key':'test-ai','model':'old'},'contacts':[{'name':'one','email_password':'test-mail','wechat_token':'test-token'}]})
        async def scenario():
            config=await app.get_config();contacts=await app.get_contacts()
            self.assertNotIn('test-ai',json.dumps(config));self.assertNotIn('test-mail',json.dumps(contacts))
            contacts[0]['name']='renamed'
            await app.update_contacts(contacts)
            self.assertEqual(app.app_state.config['contacts'][0]['email_password'],'test-mail')
            config['contacts']=contacts;config['ai_config']['model']='new'
            await app.update_full_config(config)
            self.assertEqual(app.app_state.config['ai_config']['api_key'],'test-ai')
            self.assertEqual(app.app_state.config['ai_config']['model'],'new')
        self.run_async(scenario)

    def test_duplicate_manual_and_start_requests_rejected_before_dispatch(self):
        app=self.app
        async def scenario():
            bg=app.BackgroundTasks()
            await app.run_once(bg)
            with self.assertRaises(app.HTTPException) as error:await app.run_once(app.BackgroundTasks())
            self.assertEqual(error.exception.status_code,409)
            await app.stop_monitor()
            with self.assertRaises(app.HTTPException):await app.start_monitor(app.BackgroundTasks())
            self.assertTrue(app.app_state.stop_event.is_set())
            await bg() # stopped reservation is released without ever executing a crawler.
            self.assertIsNone(app.app_state.task_token)
        self.run_async(scenario)

    def test_scheduled_and_manual_share_lock(self):
        app=self.app
        async def scenario():
            entered=asyncio.Event();release=asyncio.Event();calls=[]
            async def worker():calls.append(1);entered.set();await release.wait()
            app.app_state.continuous=app.app_state.is_running=True
            with patch.object(app,'_execute_monitor_task',worker):
                scheduled=asyncio.create_task(app.run_monitor_task());await entered.wait()
                with self.assertRaises(app.HTTPException):await app.run_once(app.BackgroundTasks())
                await app.run_monitor_task() # another scheduled trigger cannot execute.
                self.assertEqual(len(calls),1)
                release.set();await scheduled
                self.assertIsNone(app.app_state.task_token)
        self.run_async(scenario)

    def test_cancel_keeps_lock_until_worker_finishes(self):
        app=self.app
        async def scenario():
            entered=asyncio.Event();release=asyncio.Event()
            async def worker():entered.set();await release.wait()
            app.app_state.continuous=app.app_state.is_running=True
            with patch.object(app,'_execute_monitor_task',worker):
                running=asyncio.create_task(app.run_monitor_task());await entered.wait();running.cancel();await asyncio.sleep(0)
                self.assertIsNotNone(app.app_state.task_token)
                with self.assertRaises(app.HTTPException):await app.run_once(app.BackgroundTasks())
                release.set()
                with self.assertRaises(asyncio.CancelledError):await running
                self.assertIsNone(app.app_state.task_token)
        self.run_async(scenario)

    def test_manual_run_does_not_create_a_periodic_schedule(self):
        app=self.app
        async def scenario():
            async def worker():pass
            bg=app.BackgroundTasks()
            with patch.object(app,'_execute_monitor_task',worker):
                await app.run_once(bg);await bg()
            self.assertFalse(app.app_state.is_running)
            self.assertFalse(app.app_state.continuous)
            self.assertIsNone(app.app_state.scheduler)
        self.run_async(scenario)

    def test_reservation_is_atomic_across_threads(self):
        app=self.app;tokens=[]
        threads=[threading.Thread(target=lambda:tokens.append(app.reserve_monitor())) for _ in range(20)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(sum(t is not None for t in tokens),1)
        app.release_monitor(next(t for t in tokens if t is not None))

    def test_clear_requires_scope_and_confirmation_and_blocks_running_jobs(self):
        app=self.app;app.app_state.storage.save(BidInfo('offline','https://example.invalid/1','','test'))
        async def scenario():
            for scope,confirmed in [('',False),('all',False),('invalid',True)]:
                with self.assertRaises(app.HTTPException):await app.clear_history(scope,confirmed)
            token=app.reserve_monitor()
            with self.assertRaises(app.HTTPException):await app.clear_history('all',True)
            app.release_monitor(token)
            app.competitor_service.reserve(A,['ggzy'])
            with self.assertRaises(app.HTTPException):await app.clear_history('all',True)
            self.assertEqual(app.app_state.storage.count_all(),1)
            app.competitor_service.state['running']=False
            self.assertTrue((await app.clear_history('all',True))['success'])
            self.assertEqual(app.app_state.storage.count_all(),0)
        self.run_async(scenario)

    def test_monitor_core_shared_browser_lock_releases_on_error(self):
        from monitor_core import MonitorCore, _MONITOR_RUN_LOCK
        core=MonitorCore.__new__(MonitorCore);core.storage=Mock()
        core._run_once=Mock(side_effect=RuntimeError('offline-error'))
        with patch('crawler.selenium_crawler.SharedBrowserManager.close'):
            with self.assertRaises(RuntimeError):core.run_once()
            self.assertTrue(_MONITOR_RUN_LOCK.acquire(False));_MONITOR_RUN_LOCK.release()
        core.storage.close.assert_called_once()


if __name__=='__main__':unittest.main()
