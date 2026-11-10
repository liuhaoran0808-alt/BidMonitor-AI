"""公开交易数据适配。原生查询、精确企业匹配；不把业绩或公告摘要冒充开标报价。"""
from contextlib import contextmanager
import base64
import hashlib
from decimal import Decimal, InvalidOperation
from io import BytesIO
import json
import re
from urllib.parse import urlencode, urlsplit

from bs4 import BeautifulSoup
from record_identity import identity_for

OFFICIAL_DOMAINS = ('ggzy.gov.cn', 'ggzyfw.beijing.gov.cn', 'ccgp.gov.cn')
MAX_PAGES = 3


def official_url(value):
    try:
        p = urlsplit(str(value).strip())
        return (p.scheme == 'https' and not p.username and not p.password
                and p.port in (None, 443)
                and any(p.hostname == h or (p.hostname or '').endswith('.' + h) for h in OFFICIAL_DOMAINS))
    except ValueError:
        return False


def validate_documents(urls):
    values = list(dict.fromkeys(str(x).strip() for x in (urls or []) if str(x).strip()))
    if len(values) > 5 or any(not official_url(x) or len(x) > 2048 for x in values):
        raise ValueError('每次最多5个HTTPS官网公告/附件地址；支持全国公共资源交易、北京公共资源交易及中国政府采购网')
    return values


def normalize(value):
    return re.sub(r'\s+', '', value or '').replace('（', '(').replace('）', ')')


def search_url(company):
    return 'https://data.ggzy.gov.cn/#/search?' + urlencode({'keyWord': company})


def parse_trade_page(html, company, url, page=1):
    """解析已经渲染的企业成交表。只接受详情标题全称一致的企业。"""
    soup = BeautifulSoup(html, 'html.parser')
    titles = soup.select('.company .title')
    if not any(normalize(x.get_text()) == normalize(company) for x in titles):
        raise ValueError('成交页企业名称与当前查询不一致，停止保存')
    basic = {'企业名称': company}
    for item in soup.select('.company .some-info > div'):
        label = item.select_one('.label')
        if not label:
            continue
        key = label.get_text(strip=True).rstrip('：:')
        if key in ('统一社会信用代码', '成立日期', '登记状态', '注册资本', '类型'):
            basic[key] = item.get_text(' ', strip=True)[len(label.get_text(' ', strip=True)):].strip()
    records = []
    for wrapper in soup.select('.ivu-table'):
        headers = [x.get_text(' ', strip=True) for x in wrapper.select('.ivu-table-header th')]
        if not ('项目名称' in headers and '中标日期' in headers):
            continue
        for row in wrapper.select('.ivu-table-body tbody tr'):
            values = [x.get_text(' ', strip=True) for x in row.select('td')]
            fields = {h: v for h, v in zip(headers, values) if h and v and h != '序号'}
            title = fields.get('项目名称')
            if not title:
                continue
            identity, strength = identity_for({'title': title, 'fields': fields})
            fields['身份依据'] = strength
            fields['身份说明'] = '源站编号' if strength == 'source' else '缺少稳定标段ID；不同金额仅作待核实版本，不自动算新增项目'
            fields.update({'查询企业': company, '记录类别': '中标（成交）结果', '企业匹配': '企业全称精确匹配',
                           '源站页码': str(page), '金额说明': '保留源站单位；0或缺失金额不代表免费，须核对公告原文',
                           '来源类型': '全国平台企业成交汇总页',
                           '公告检索地址': 'https://www.ggzy.gov.cn/deal/dealList.html?' + urlencode({'FINDTXT': title})})
            if basic.get('统一社会信用代码'):
                fields['统一社会信用代码'] = basic['统一社会信用代码']
            records.append({'kind': 'tender', 'title': title, 'source_url': url,
                            'fields': fields, 'identity': identity})
    return basic, records


@contextmanager
def own_detail(driver, element, wait):
    parent, handles, old_url = driver.current_window_handle, set(driver.window_handles), driver.current_url
    try:
        element.click()
        wait.until(lambda d: d.current_url != old_url or bool(set(d.window_handles) - handles))
        children = set(driver.window_handles) - handles
        if children:
            driver.switch_to.window(next(iter(children)))
        if urlsplit(driver.current_url).hostname != 'data.ggzy.gov.cn':
            raise ValueError('企业详情离开指定官网，停止读取')
        yield
    finally:
        for handle in list(driver.window_handles):
            if handle not in handles:
                driver.switch_to.window(handle)
                driver.close()
        if parent in driver.window_handles:
            driver.switch_to.window(parent)


def query_public_trades(driver, company, check_access, stop=None, timeout=25, max_pages=MAX_PAGES):
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.common.exceptions import TimeoutException, StaleElementReferenceException
    wait = WebDriverWait(driver, timeout, ignored_exceptions=(StaleElementReferenceException,))
    driver.get(search_url(company))

    def exact(d):
        check_access(d)
        # 不使用高亮span，否则会把“河南某公司”中的子串误认作同一个主体。
        return [e for e in d.find_elements(By.CSS_SELECTOR, '.company .title, .company-item .title, .data-item .title, .title')
                if e.is_displayed() and normalize(e.text) == normalize(company)]
    try:
        candidates = wait.until(exact)
    except TimeoutException:
        check_access(driver)
        return [], 'query_failed', '未定位到精确企业主体；可能是无匹配、加载失败或名称差异，不自动判定企业无记录'
    # CSS的并集会去重同一个元素；多个同名主体需人工确认信用代码，不能随意选择。
    if len(candidates) != 1:
        return [], 'query_failed', '发现多个同名主体，需按统一社会信用代码核实，未自动合并'
    if stop and stop.is_set():
        return [], 'stopped', '已停止'
    records, pages = [], 0
    try:
        with own_detail(driver, candidates[0], wait):
            try:
                def loaded(d):
                    check_access(d)
                    return d.find_elements(By.CSS_SELECTOR, '.ivu-table-body tbody tr')
                wait.until(loaded)
            except TimeoutException:
                check_access(driver)
                return [], 'query_failed', '企业已定位，但未确认成交列表加载成功；未把加载中的空白当作无结果'
            detail = driver.current_url
            for page in range(1, min(max(int(max_pages), 1), 10) + 1):
                if stop and stop.is_set():
                    return records, 'stopped', '已停止，已读取记录保留'
                check_access(driver)
                basic, rows = parse_trade_page(driver.page_source, company, detail, page)
                if not rows:
                    break
                if not records:
                    records.append({'kind': 'company', 'title': company, 'source_url': detail, 'fields': basic})
                records.extend(rows)
                pages += 1
                next_buttons = [e for e in driver.find_elements(By.CSS_SELECTOR, '.ivu-page-next') if e.is_displayed()]
                if not next_buttons or 'ivu-page-disabled' in (next_buttons[0].get_attribute('class') or ''):
                    break
                if page >= max_pages:
                    break
                first = driver.find_elements(By.CSS_SELECTOR, '.ivu-table-body tbody tr')[0].text
                next_buttons[0].click()
                def changed(d):
                    if stop and stop.is_set():
                        raise InterruptedError('已请求停止')
                    check_access(d)
                    active = d.find_elements(By.CSS_SELECTOR, '.ivu-page-item-active')
                    rows = d.find_elements(By.CSS_SELECTOR, '.ivu-table-body tbody tr')
                    return active and active[0].text.strip() == str(page + 1) and rows and rows[0].text != first
                try:
                    wait.until(changed)
                except TimeoutException:
                    return records, 'partial', f'已读取{pages}页成交记录，下一页未能确认加载，未继续猜测结果'
    except Exception as exc:
        if not records:
            raise
        status = 'stopped' if stop and stop.is_set() else getattr(exc, 'status', 'site_error' if hasattr(exc, 'code') else 'partial')
        return records, status, f'已保留{pages}页记录；后续读取中断（{type(exc).__name__}），并非完整结果'
    return records, 'completed', f'已读取{pages}页中标（成交）记录（每次最多{max_pages}页）；不是完整开标报价，金额以原公告为准'


def parse_opening_text(text, company, url, title=''):
    """保守识别公开文件的开标表：同一节须有明确表头及企业报价行。"""
    if not official_url(url):
        raise ValueError('不是支持的官网地址')
    compact = normalize(text)
    company_key = normalize(company)
    if company_key not in compact:
        return []
    project = re.search(r'招标项目名称\s*[:：]\s*([^\n]+)', text)
    project_id = re.search(r'(?:招标)?项目编号\s*[:：]\s*([A-Za-z0-9_-]+)', text)
    section = re.search(r'开标记录(.*?)(?=二[、.．]|投标文件被判定|评标专家|$)', text, re.S)
    if not section:
        return []
    part = section.group(1)
    # 表头顺序必须是企业名称后紧接报价，避免把其他金额/评分误认为投标报价。
    header = re.search(r'投标(?:单位|人)名称\s*投标(?:总)?报价\s*[（(]?\s*(元|万元)', part)
    if not header:
        return []
    name_pattern = r'\s*'.join(re.escape(ch) for ch in company_key)
    row_text = part[header.end():].replace('（', '(').replace('）', ')')
    # 仅在行首（可带序号）匹配企业；保留报价后的空白，不能把“6个月”的6拼进金额。
    row = re.search(r'(?:^|\n)\s*(?:\d+\s+)?' + name_pattern + r'\s+([^\s]+)', row_text)
    if not row:
        return []
    token = re.fullmatch(r'([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)(万元|元)?', row.group(1))
    if not token or (token.group(2) and token.group(2) != header.group(1)):
        return []
    amount = token.group(1)
    # Require valid thousands grouping rather than accepting arbitrary commas.
    if ',' in amount and not re.fullmatch(r'[1-9][0-9]{0,2}(?:,[0-9]{3})+(?:\.[0-9]+)?', amount):
        return []
    try:
        if not Decimal(amount.replace(',', '')).is_finite():
            return []
    except InvalidOperation:
        return []
    fields = {'查询企业': company, '投标人': company, '投标报价': amount, '报价单位': header.group(1),
              '记录类别': '开标记录', '提取依据': '官网附件开标记录表中的企业报价行'}
    if project_id:
        fields['项目编号'] = project_id.group(1)
    date = re.search(r'开标时间\s*[:：]\s*([^\n]+)', text)
    if date:
        fields['开标时间'] = date.group(1).strip()
    return [{'kind': 'opening', 'title': project.group(1).strip() if project else (title or '开标记录'),
             'source_url': url, 'fields': fields,
             'identity': identity_for({'title': project.group(1).strip() if project else (title or '开标记录'), 'fields': fields})[0]}]


def query_official_document(driver, company, url, check_access):
    """补充公告/附件入口，地址由用户提供；绝不硬编码某家公司的样本链接。"""
    validate_documents([url])
    if urlsplit(url).path.lower().endswith('.pdf'):
        from pypdf import PdfReader
        p = urlsplit(url)
        driver.get(p.scheme + '://' + p.netloc + '/')
        check_access(driver)
        driver.set_script_timeout(30)
        payload = driver.execute_async_script("""const done=arguments[arguments.length-1];
          fetch(arguments[0],{redirect:'error',signal:AbortSignal.timeout(25000)})
          .then(async r=>{if(!r.ok)throw Error('HTTP '+r.status);let a=new Uint8Array(await r.arrayBuffer());
           if(a.length>10*1024*1024)throw Error('附件超过10MB');
           let s='';for(let i=0;i<a.length;i+=8192)s+=String.fromCharCode(...a.subarray(i,i+8192));
           done({data:btoa(s)});}).catch(e=>done({error:e.message}));""", url)
        if payload.get('error'):
            raise ValueError('官网附件读取失败：' + payload['error'])
        raw = base64.b64decode(payload['data'])
        if not raw.startswith(b'%PDF'):
            raise ValueError('返回内容不是PDF，可能是验证页，未保存')
        reader = PdfReader(BytesIO(raw))
        if reader.is_encrypted:
            raise ValueError('PDF加密，未自动处理')
        if len(reader.pages) > 30:
            raise ValueError('附件超过30页，未自动解析')
        text = '\n'.join(p.extract_text() or '' for p in reader.pages)
        digest = hashlib.sha256(raw).hexdigest()
    else:
        driver.get(url)
        check_access(driver)
        if not official_url(driver.current_url):
            raise ValueError('公告跳转离开支持的官网，未保存')
        text = BeautifulSoup(driver.page_source, 'html.parser').get_text('\n', strip=True)
        digest = hashlib.sha256(text.encode()).hexdigest()
    rows = parse_opening_text(text, company, url)
    for row in rows:
        row['fields']['来源内容SHA256'] = digest
    return rows, 'completed' if rows else 'unconfirmed', (
        '已核对官网开标表中的企业及报价行' if rows else '未识别到该企业的明确开标报价行；不将普通提及、合同金额或评分当作开标记录')
