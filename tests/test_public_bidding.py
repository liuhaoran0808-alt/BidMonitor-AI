"""离线回归：不访问源站，业务库不参与测试。"""
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT=Path(os.environ.get('BIDMONITOR_TEST_ROOT',Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(ROOT/'src'))
import public_bidding as p
spec=importlib.util.spec_from_file_location('_public_competitor_test',ROOT/'src/competitor.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

A='测试企业甲有限公司'
B='测试企业乙有限公司'
URL='https://data.ggzy.gov.cn/#/company?id=TESTCODE'
PDF='https://ggzyfw.beijing.gov.cn/cmsbj/test.pdf'

def fixture(company=A):
    return f'''<div class="company"><div class="title">{company}</div><div class="some-info"><div><span class="label">统一社会信用代码：</span><span>TESTCODE</span></div></div></div>
    <div class="ivu-table"><div class="ivu-table-header"><table><thead><tr><th>序号</th><th>招标（采购）人</th><th>项目名称</th><th>交易领域</th><th>中标日期</th><th>中标（成交）金额（万元）</th></tr></thead></table></div>
    <div class="ivu-table-body"><table><tbody><tr><td>1</td><td>测试采购单位</td><td>测试信息化项目</td><td>政府采购</td><td>2026-11-09</td><td>96.00</td></tr></tbody></table></div></div>'''

def opening():
    return f'''招标项目编号：TEST-001 招标项目名称：测试系统建设项目
一、开标记录
序号 投标单位名称 投标总报价
(元) 项目完成期
1 {A} 8671000.00 6个月
2 {B} 8731800.00 6个月
二、投标文件被判定为废标的投标人名称、废标原因
三、评标专家评分 {A} 98.00'''

class PublicBiddingTests(unittest.TestCase):
    def setUp(self):
        temp=os.environ.get('BIDMONITOR_TEST_TEMP',tempfile.gettempdir())
        self.tmp=tempfile.TemporaryDirectory(dir=temp,prefix='public-bid-test-')
        self.addCleanup(self.tmp.cleanup)
        self.db=str(Path(self.tmp.name)/'test.db')

    def test_trade_fields_preserve_original_units_and_are_not_opening(self):
        basic,rows=p.parse_trade_page(fixture(),A,URL)
        self.assertEqual(basic['统一社会信用代码'],'TESTCODE')
        self.assertEqual(rows[0]['kind'],'tender')
        self.assertEqual(rows[0]['fields']['中标（成交）金额（万元）'],'96.00')
        self.assertNotIn('投标报价',rows[0]['fields'])

    def test_wrong_and_prefix_company_are_rejected(self):
        for title in [B,'河南'+A]:
            with self.assertRaises(ValueError):p.parse_trade_page(fixture(title),A,URL)

    def test_record_identity_does_not_depend_on_page_position(self):
        a=p.parse_trade_page(fixture(),A,URL,1)[1][0]
        b=p.parse_trade_page(fixture(),A,URL,2)[1][0]
        self.assertEqual(a['identity'],b['identity'])
        self.assertNotEqual(a['fields']['源站页码'],b['fields']['源站页码'])

    def test_pdf_current_company_selects_its_own_quote(self):
        a=p.parse_opening_text(opening(),A,PDF)[0]
        b=p.parse_opening_text(opening(),B,PDF)[0]
        self.assertEqual(a['fields']['投标报价'],'8671000.00')
        self.assertEqual(b['fields']['投标报价'],'8731800.00')
        self.assertEqual(a['fields']['报价单位'],'元')
        self.assertEqual(a['fields']['项目编号'],'TEST-001')
        self.assertEqual(a['title'],'测试系统建设项目')
        self.assertNotIn('开标时间',a['fields'])

    def test_contract_or_score_is_not_an_opening_quote(self):
        for t in [A+' 合同金额100万元',A+' 得分98.00',A+' 中标金额100万元',opening().replace('投标总报价','合同金额')]:
            self.assertEqual(p.parse_opening_text(t,A,PDF),[])

    def test_company_name_mention_without_a_quote_does_not_prove_opening(self):
        self.assertEqual(p.parse_opening_text('开标记录 投标单位名称 投标总报价(元) '+A,A,PDF),[])

    def test_prefix_company_does_not_match_original_company_quote(self):
        self.assertEqual(p.parse_opening_text(opening().replace(A,'河南'+A),A,PDF),[])

    def test_opening_table_may_wrap_company_name_across_lines(self):
        text=opening().replace(A,A[:4]+'\n'+A[4:])
        self.assertEqual(p.parse_opening_text(text,A,PDF)[0]['fields']['投标报价'],'8671000.00')

    def test_official_links_are_restricted_and_do_not_accept_credentials_or_private_hosts(self):
        for url in [PDF,URL,'https://www.ccgp.gov.cn/cggg/test.htm']:
            self.assertTrue(p.official_url(url))
        for url in ['http://127.0.0.1/test','https://localhost/','file:///C:/secret','https://ggzy.gov.cn.evil.test/x','https://user:pass@www.ggzy.gov.cn/x','https://www.ggzy.gov.cn:bad/x','javascript:alert(1)']:
            self.assertFalse(p.official_url(url))
            with self.assertRaises(ValueError):p.validate_documents([url])

    def test_too_many_documents_rejected_and_duplicates_removed(self):
        self.assertEqual(p.validate_documents([PDF,PDF]),[PDF])
        with self.assertRaises(ValueError):p.validate_documents([PDF+str(i) for i in range(6)])

    def test_summary_counts_all_saved_rows_not_only_display_limit(self):
        store=m.Records(self.db)
        for i in range(205):
            store.save(A,'water',{'kind':'project','title':'测试项目'+str(i),'source_url':'https://scjg.mwr.gov.cn/','fields':{}})
        summary=store.summary([A])
        self.assertEqual(summary['type_counts']['project'],205)
        self.assertEqual(summary['company_count'],1)
        self.assertEqual(len(store.list([A],limit=200)),200)

    def test_summary_keeps_company_boundaries_and_zero_companies(self):
        store=m.Records(self.db)
        for name in [A,B]:store.save(name,'water',{'kind':'project','title':'同名测试项目','source_url':'https://scjg.mwr.gov.cn/','fields':{}})
        summary=store.summary([A,B,'暂无记录企业'])
        self.assertEqual(summary['company_count'],2)
        self.assertEqual(len(summary['groups']),3)
        self.assertEqual(summary['groups'][-1]['counts']['project'],0)

    def test_amount_change_without_lot_id_is_review_not_new_project(self):
        store=m.Records(self.db)
        a=p.parse_trade_page(fixture(),A,URL)[1][0]
        b=p.parse_trade_page(fixture().replace('96.00','97.00'),A,URL)[1][0]
        self.assertTrue(store.save(A,'ggzy',a));self.assertFalse(store.save(A,'ggzy',a))
        self.assertFalse(store.save(A,'ggzy',b))
        self.assertEqual(store.counts()['total'],1)
        self.assertEqual(store.counts()['review_count'],1)
        item=store.list()[0]
        self.assertEqual(item['fields']['中标（成交）金额（万元）'],'96.00')
        self.assertEqual(item['observations'][0]['fields']['中标（成交）金额（万元）'],'97.00')

    def test_reserve_preserves_source_query_and_adds_document_work_items(self):
        service=m.CompetitorService(self.db)
        state=service.reserve(A,['ggzy'],[PDF])
        self.assertEqual(state['total'],2)
        self.assertEqual(state['entries'][1]['document_url'],PDF)
        self.assertIn('keyWord=',state['entries'][0]['query_url'])

    def test_summary_and_rows_name_the_actual_pdf_source(self):
        store=m.Records(self.db)
        store.save(A,'ggzy',p.parse_opening_text(opening(),A,PDF)[0])
        self.assertEqual(store.list([A])[0]['source'],'北京市公共资源交易服务平台')
        self.assertEqual(store.summary([A])['groups'][0]['sources'][0]['name'],'北京市公共资源交易服务平台')

    def test_invalid_document_rejected_before_reserving_job(self):
        service=m.CompetitorService(self.db)
        with self.assertRaises(ValueError):service.reserve(A,['ggzy'],['https://127.0.0.1/'])
        self.assertFalse(service.snapshot()['running'])

    def test_document_without_matching_quote_does_not_save_a_fake_record(self):
        service=m.CompetitorService(self.db)
        @contextmanager
        def session(platform):yield object()
        service.sessions.use=session;service.reserve(A,['ggzy'],[PDF])
        with patch.object(m,'query_platform',return_value=([],'unconfirmed','测试')),patch.object(p,'query_official_document',return_value=([],'unconfirmed','没有明确报价')):
            service.run()
        self.assertEqual(service.records.counts()['total'],0)
        self.assertTrue(all(e['status']=='unconfirmed' for e in service.snapshot()['entries']))

    @unittest.skipUnless(shutil.which('node'),'Node required')
    def test_frontend_groups_and_raw_field_labels_are_safe(self):
        js=r'''const fs=require('fs'),vm=require('vm'),assert=require('assert');
const context=vm.createContext({URL,escapeHtml:s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
const r={company:'甲公司',record_type:'project',title:'<script>bad</script>',url:'javascript:alert(1)',source:'官网',fields:{'合同金额(万元)':'99.8','开工日期':'2021-01-01'},collected_at:'2026-11-10'};
const result={items:[r],display_limit:200,summary:{groups:[{company:'甲公司',sources:[{name:'官网'}],counts:{company:0,project:1,opening:0,tender:0},latest:'2026-11-10'}]}};
const html=context.renderCompetitorGroups(result);
assert(html.includes('&lt;script&gt;'));assert(!html.includes('<script>bad'));
assert(html.includes('合同金额(万元)'));assert(html.includes('开工日期'));
assert(html.includes('href="#"'));assert(!html.includes('href="javascript:'));
'''
        run=subprocess.run([shutil.which('node'),'-e',js,str(ROOT/'server/static/competitors.js')],capture_output=True,text=True,encoding='utf-8',timeout=15)
        self.assertEqual(run.returncode,0,run.stderr)

if __name__=='__main__':unittest.main()
