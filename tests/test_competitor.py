"""动态关键词、六站查询地址、结构化项目、状态、去重和界面的离线测试。"""
from contextlib import contextmanager
import ast
import copy
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
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, unquote, urlsplit

ROOT=Path(os.environ.get('BIDMONITOR_TEST_ROOT',Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(ROOT/'src'))
spec=importlib.util.spec_from_file_location('_competitor_under_test',ROOT/'src/competitor.py')
m=importlib.util.module_from_spec(spec); sys.modules[spec.name]=m; spec.loader.exec_module(m)
A='浪潮软件股份有限公司'
B='浪潮软件集团有限公司'


class CompetitorTests(unittest.TestCase):
    def setUp(self):
        root=Path(os.environ.get('BIDMONITOR_TEST_TEMP',tempfile.gettempdir())).resolve()
        self.temporary=tempfile.TemporaryDirectory(prefix='competitor-test-',dir=str(root))
        self.assertEqual(Path(self.temporary.name).resolve().parent,root)
        self.addCleanup(self.temporary.cleanup)
        self.db=str(Path(self.temporary.name)/'test.db')

    def test_original_six_platforms_preserved_and_public_trades_added(self):
        self.assertEqual(set(m.PLATFORMS),{'gsxt','creditchina','jzsc','water','water_credit','road','ggzy'})

    def test_queries_use_current_full_company_not_fixed_demo(self):
        for platform in set(m.PLATFORMS)-{'gsxt'}:
            first,second=m.search_url(platform,A),m.search_url(platform,B)
            self.assertNotEqual(first,second)
            self.assertIn(A,unquote(unquote(first)))
            self.assertIn(B,unquote(unquote(second)))
            self.assertNotIn(A,unquote(unquote(second)))

    def test_jzsc_overrides_company_in_new_request(self):
        query=parse_qs(urlsplit(m.search_url('jzsc',B)).query)
        self.assertEqual(query['complexname'],[B])

    def test_water_uses_hash_route_query(self):
        parts=urlsplit(m.search_url('water',B))
        self.assertTrue(parts.fragment.startswith('/cydw/main?'))
        self.assertEqual(parse_qs(parts.fragment.split('?',1)[1])['keywords'],[B])

    def test_road_preserves_observed_double_encoding(self):
        value=parse_qs(urlsplit(m.search_url('road',B)).query)['text'][0]
        self.assertNotEqual(value,B)
        self.assertEqual(unquote(value),B)

    def test_credit_keeps_official_query_parameters(self):
        q=parse_qs(urlsplit(m.search_url('creditchina',A)).query)
        self.assertEqual(q['keyword'],[A]); self.assertEqual(q['tableName'],['credit_xyzx_tyshxydm'])

    def test_validation_and_deduplication(self):
        self.assertEqual(m.parse_companies(A+'\n'+B+'\n'+A),[A,B])
        for bad in ('', '\n', '\n'.join('测试企业'+str(i) for i in range(6)), 'x'*129):
            with self.assertRaises(ValueError): m.parse_companies(bad)
        with self.assertRaises(ValueError): m.validate_platforms(['unknown'])
        with self.assertRaises(ValueError): m.validate_platforms([])

    def test_sql_input_cannot_become_sql_or_xpath_code(self):
        text='测试企业\'"有限公司'
        self.assertTrue(m.xpath_text(text).startswith('concat('))
        store=m.Records(self.db)
        self.assertTrue(store.save(text,'water',{'kind':'company','title':text,'source_url':m.PLATFORMS['water']['url'],'fields':{}}))
        self.assertEqual(store.list([text])[0]['company'],text)

    def test_observed_water_project_columns_not_renamed_to_opening(self):
        headers=['','','序号','项目名称','项目归属','项目状态','合同金额(万元)','开工日期','建设单位','信息完整度','']
        rows=[['','', '1','广东商务大数据共享分析平台建设项目（软件系统开发）','山东省/济南市','完工已验收','1205.66','2017-09-18',A,'63']]
        record=m.project_records(headers,rows,m.search_url('water',A),A)[0]
        self.assertEqual(record['kind'],'project')
        self.assertEqual(record['fields']['合同金额(万元)'],'1205.66')
        self.assertEqual(record['fields']['开工日期'],'2017-09-18')
        self.assertNotIn('投标报价',record['fields'])
        self.assertNotIn('开标时间',record['fields'])

    def test_true_opening_and_award_fields_are_separate_types(self):
        row=m.project_records(['项目名称','开标时间','投标报价'],[['测试标段','2026-11-01','100万元']],m.search_url('water',A),A)[0]
        self.assertEqual(row['kind'],'opening')
        row=m.project_records(['项目名称','中标金额'],[['测试项目','100万元']],m.search_url('water',A),A)[0]
        self.assertEqual(row['kind'],'tender')

    def test_storage_keeps_add_time_on_duplicate_and_filters_company(self):
        store=m.Records(self.db)
        record={'kind':'project','title':'本地测试项目','source_url':m.search_url('water',A),'fields':{'合同金额':'1'}}
        self.assertTrue(store.save(A,'water',record))
        first=store.list([A])[0]['collected_at']
        self.assertFalse(store.save(A,'water',dict(record,fields={'合同金额':'2'})))
        self.assertEqual(store.list([A])[0]['collected_at'],first)
        self.assertEqual(store.list([A])[0]['fields']['合同金额'],'2')
        self.assertEqual(store.list([B]),[])
        self.assertEqual(store.counts([A]),{'total':1,'today_new':1,'review_count':0})

    def test_rejects_unverified_external_record_url(self):
        with self.assertRaises(ValueError):
            m.Records(self.db).save(A,'water',{'kind':'company','title':A,'source_url':'javascript:alert(1)','fields':{}})

    def test_service_resets_query_state_for_new_company(self):
        service=m.CompetitorService(self.db)
        service.reserve(A,['water']); service.state['running']=False
        state=service.reserve(B,['water','jzsc'])
        self.assertEqual(state['companies'],[B])
        self.assertTrue(all(e['company']==B and A not in unquote(e['query_url']) for e in state['entries']))
        with self.assertRaises(ValueError): service.reserve(A,['water'])

    def test_captcha_is_manual_status_not_no_results(self):
        driver=Mock(); driver.title='Environment Checking'; driver.find_elements.return_value=[]
        with self.assertRaises(m.ManualRequired): m._check_access(driver)
        service=m.CompetitorService(self.db)
        @contextmanager
        def session(platform): yield Mock()
        service.sessions.use=session
        service.reserve(A,['creditchina'])
        with patch.object(m,'query_platform',side_effect=m.ManualRequired('需要验证码')): service.run()
        self.assertEqual(service.snapshot()['entries'][0]['status'],'verification_required')
        self.assertEqual(service.records.counts()['total'],0)

    def test_records_saved_and_company_not_counted_as_opening(self):
        service=m.CompetitorService(self.db)
        @contextmanager
        def session(platform): yield Mock()
        service.sessions.use=session
        rows=[{'kind':'company','title':A,'source_url':m.search_url('water',A),'fields':{}},
              {'kind':'project','title':'测试工程','source_url':m.search_url('water',A),'fields':{}}]
        service.reserve(A,['water'])
        with patch.object(m,'query_platform',return_value=(rows,'completed','ok')): service.run()
        entry=service.snapshot()['entries'][0]
        self.assertEqual((entry['result_count'],entry['new_count'],entry['opening_count']),(2,2,0))
        self.assertFalse(service.snapshot()['running'])

    def test_stop_prevents_more_source_queries(self):
        service=m.CompetitorService(self.db); service.reserve(A,['water','road']); service.stop.set()
        with patch.object(m,'query_platform') as query: service.run()
        query.assert_not_called()
        self.assertTrue(all(e['status']=='stopped' for e in service.snapshot()['entries']))

    def test_headless_driver_closed_but_manual_session_retained(self):
        sessions=m.Sessions(); driver=Mock(); sessions.create=Mock(return_value=driver)
        with sessions.use('water'): pass
        driver.quit.assert_called_once()
        driver=Mock(); driver.window_handles=['owned']; sessions.manual['water']=driver
        with sessions.use('water'): pass
        driver.quit.assert_not_called()
        sessions.close(); driver.quit.assert_called_once()

    def test_manual_browser_receives_new_company_not_old_url(self):
        sessions=m.Sessions(); driver=Mock(); driver.window_handles=['owned']; driver.title='官网首页'; driver.find_elements.return_value=[]
        sessions.create=Mock(return_value=driver)
        sessions.open_manual('water',A)
        sessions.open_manual('water',B)
        sessions.create.assert_called_once_with(visible=True)
        self.assertEqual(driver.get.call_args.args[0],m.PLATFORMS['water']['url'])

    def test_manual_window_remains_available_on_slow_page(self):
        from selenium.common.exceptions import TimeoutException
        sessions=m.Sessions(); driver=Mock(); driver.window_handles=['owned']; driver.title='官网首页'; driver.find_elements.return_value=[]
        driver.get.side_effect=TimeoutException('test-only timeout')
        sessions.create=Mock(return_value=driver)
        sessions.open_manual('gsxt',A)
        self.assertIs(sessions.manual['gsxt'],driver)
        driver.quit.assert_not_called()

    def test_explicit_login_message_is_not_zero_results(self):
        driver=Mock(); driver.title='企业详情'
        element=Mock(); element.text='请登录后查看工程业绩'; element.is_displayed.return_value=True
        driver.find_elements.side_effect=lambda by,selector: [element] if selector=='body' else []
        with self.assertRaises(m.ManualRequired) as error: m._check_access(driver)
        self.assertEqual(error.exception.status,'login_required')

    def test_api_wiring_returns_current_company_results_and_reserves_new_query(self):
        tree=ast.parse((ROOT/'server/app.py').read_text(encoding='utf-8-sig'))
        wanted={'competitor_config','competitor_query','competitor_status','competitor_stop','competitor_results'}
        functions=[copy.deepcopy(n) for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name in wanted]
        self.assertEqual(len(functions),5)
        for node in functions: node.decorator_list=[]
        service=m.CompetitorService(self.db)
        service.records.save(A,'water',{'kind':'company','title':A,'source_url':m.search_url('water',A),'fields':{}})
        class HttpError(Exception):
            def __init__(self,status_code,detail): self.status_code,self.detail=status_code,detail
        ns={'competitor_service':service,'app_state':SimpleNamespace(config={}),
            'PLATFORMS':m.PLATFORMS,'parse_companies':m.parse_companies,
            'CompetitorQueryRequest':object,'BackgroundTasks':object,
            'HTTPException':HttpError,'save_config':Mock()}
        ns['commit_config']=lambda changes:ns['app_state'].config.update(changes)
        exec(compile(ast.Module(body=functions,type_ignores=[]),'extracted_competitor_api','exec'),ns)
        def run(coro):
            try: coro.send(None)
            except StopIteration as done: return done.value
            else: coro.close(); self.fail('unexpected async IO in isolated test')
        self.assertEqual(run(ns['competitor_results'](B))['items'],[])
        self.assertEqual(run(ns['competitor_results'](A))['items'][0]['company'],A)
        background=Mock()
        state=run(ns['competitor_query'](SimpleNamespace(company_text=B,platforms=['water']),background))
        self.assertEqual(state['companies'],[B])
        self.assertEqual(ns['app_state'].config['competitor_companies'],B)
        background.add_task.assert_called_once()
        self.assertEqual(len(run(ns['competitor_config']())['available']),7)
        run(ns['competitor_stop']()); self.assertTrue(service.stop.is_set())

    @unittest.skipUnless(shutil.which('node'),'Node required')
    def test_frontend_syntax_dynamic_input_and_safe_links(self):
        script=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const js=fs.readFileSync(process.argv[1],'utf8'); new vm.Script(js);
const context=vm.createContext({URL,escapeHtml:s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;')});
vm.runInContext(js,context);
assert.strictEqual(context.competitorSafeURL('javascript:alert(1)'),'#');
assert(context.competitorSafeURL('https://scjg.mwr.gov.cn/#/home').startsWith('https://'));
assert(js.includes("document.getElementById('cmpCompany').value.trim()"));
assert(!js.includes('浪潮软件股份有限公司'));
'''
        p=ROOT/'server/static/competitors.js'
        result=subprocess.run([shutil.which('node'),'-e',script,str(p)],capture_output=True,text=True,encoding='utf-8',timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)



    def test_521_error_is_not_a_captcha_or_empty_result(self):
        driver=Mock(); driver.title='Error 521'; driver.find_elements.return_value=[]
        with self.assertRaises(m.SiteAccessError) as error: m._check_access(driver)
        self.assertEqual(error.exception.code,521)
        state=m._manual_page_state(driver)
        self.assertEqual(state['page_state'],'site_error')
        self.assertEqual(state['site_code'],521)

    def test_numeric_project_id_is_not_a_521_error_page(self):
        driver=Mock(); driver.title='企业详情'; element=Mock(); element.is_displayed.return_value=True
        element.text='项目编号521，合同金额521万元'
        driver.find_elements.side_effect=lambda by,selector: [element] if selector=='body' else []
        self.assertIsNone(m._site_error_code(driver))
        m._check_access(driver)

    def test_manual_home_is_not_reported_as_verified_success(self):
        service=m.CompetitorService(self.db); service.reserve(A,['water']); service.state['running']=False
        service.state['entries'][0]['status']='verification_required'
        result={'opened':True,'page_state':'ready','message':'官网已打开，尚未验证'}
        service.sessions.open_manual=Mock(return_value=result)
        self.assertEqual(service.open_manual('water',A),result)
        self.assertEqual(service.snapshot()['entries'][0]['status'],'verification_required')
        self.assertFalse(service.snapshot()['manual_opening'])

    def test_manual_navigation_does_not_block_status_lock(self):
        import threading
        service=m.CompetitorService(self.db)
        opened,release=threading.Event(),threading.Event()
        def slow_open(platform,company):
            opened.set(); release.wait(3)
            return {'opened':True,'page_state':'ready','message':'未验证'}
        service.sessions.open_manual=slow_open
        worker=threading.Thread(target=service.open_manual,args=('water',A)); worker.start()
        try:
            self.assertTrue(opened.wait(1))
            self.assertTrue(service.snapshot()['manual_opening'])
            with self.assertRaises(ValueError): service.reserve(A,['water'])
        finally:
            release.set(); worker.join(3)
        self.assertFalse(worker.is_alive())

    def test_site_error_job_state_is_not_no_results(self):
        service=m.CompetitorService(self.db)
        @contextmanager
        def session(platform): yield Mock()
        service.sessions.use=session; service.reserve(A,['water'])
        with patch.object(m,'query_platform',side_effect=m.SiteAccessError(521)): service.run()
        state=service.snapshot()['entries'][0]
        self.assertEqual(state['status'],'site_error')
        self.assertEqual(state['site_code'],521)
        self.assertEqual(service.records.counts()['total'],0)

    def test_frontend_manual_button_only_for_actual_auth_states(self):
        js=(ROOT/'server/static/competitors.js').read_text(encoding='utf-8-sig')
        self.assertIn("const manual = ['login_required','verification_required'].includes(e.status)",js)
        self.assertIn('官网首页（普通浏览器）',js)

if __name__=='__main__': unittest.main()
