"""在 BidMonitor 内按企业全称查询原六个平台及公开招投标数据。仅读取正常页面，不绕过验证。"""
from contextlib import contextmanager, closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import sqlite3
import threading
from urllib.parse import urlencode, urljoin, urlsplit, quote

TZ = timezone(timedelta(hours=8))
PLATFORMS = {
    'ggzy': {'name': '全国公共资源交易平台－中标成交', 'url': 'https://data.ggzy.gov.cn/', 'domain': 'ggzy.gov.cn', 'tabs': (), 'domains': ('ggzy.gov.cn', 'ggzyfw.beijing.gov.cn', 'ccgp.gov.cn')},
    'gsxt': {'name': '国家企业信用信息公示系统', 'url': 'https://www.gsxt.gov.cn/index.html', 'domain': 'gsxt.gov.cn', 'tabs': ()},
    'creditchina': {'name': '信用中国', 'url': 'https://www.creditchina.gov.cn/', 'domain': 'creditchina.gov.cn', 'tabs': ()},
    'jzsc': {'name': '全国建筑市场监管公共服务平台（四库一平台）', 'url': 'https://jzsc.mohurd.gov.cn/', 'domain': 'jzsc.mohurd.gov.cn', 'tabs': ('工程项目',)},
    'water': {'name': '全国水利建设市场监管平台', 'url': 'https://scjg.mwr.gov.cn/', 'domain': 'scjg.mwr.gov.cn', 'tabs': ('业绩信息',)},
    'water_credit': {'name': '全国水利建设市场信用信息平台', 'url': 'https://rcpu.cweun.org/', 'domain': 'rcpu.cweun.org', 'tabs': ('工程业绩',)},
    'road': {'name': '全国公路建设市场监督管理查询系统', 'url': 'https://hwdms.mot.gov.cn/BMWebSite/', 'domain': 'hwdms.mot.gov.cn', 'tabs': ('工程业绩', '施工业绩', '已建项目')},
}


def parse_companies(text):
    names = list(dict.fromkeys(x.strip() for x in re.split(r'[\r\n,，;；]+', text or '') if x.strip()))
    if not names or len(names) > 5 or any(len(x) > 128 for x in names):
        raise ValueError('请填写企业全称，一行一家；每次最多5家，每个名称不超过128字')
    return names


def validate_platforms(ids):
    ids = list(dict.fromkeys(ids or []))
    if not ids or any(x not in PLATFORMS for x in ids):
        raise ValueError('请选择至少一个支持的平台')
    return ids


def search_url(platform, company):
    """每次由本次关键词生成地址，绝不复用某家公司的固定结果页。"""
    if platform == 'ggzy':
        from public_bidding import search_url as trade_search_url
        return trade_search_url(company)
    if platform == 'jzsc':
        return 'https://jzsc.mohurd.gov.cn/data/company?' + urlencode({'complexname': company})
    if platform == 'water':
        return 'https://scjg.mwr.gov.cn/#/cydw/main?' + urlencode({'keywords': company})
    if platform == 'water_credit':
        return 'https://rcpu.cweun.org/Home/search?' + urlencode({'kw': company})
    if platform == 'road':
        # 官网实际提交两次编码，不能只编码一次。
        return 'https://hwdms.mot.gov.cn/BMWebSite/company/indexSearch.do?' + urlencode({'text': quote(company, safe='')})
    if platform == 'creditchina':
        return 'https://www.creditchina.gov.cn/xinyongxinxi/index.html?' + urlencode({
            'index': 0, 'scenes': 'defaultScenario', 'tableName': 'credit_xyzx_tyshxydm',
            'searchState': 2, 'entityType': '1,2,4,5,6,7,8', 'keyword': company})
    if platform == 'gsxt':
        return PLATFORMS[platform]['url']
    raise ValueError('未知平台')


def record_source_name(platform, url):
    host = urlsplit(url).hostname or ''
    if platform == 'ggzy':
        if host == 'ggzyfw.beijing.gov.cn': return '北京市公共资源交易服务平台'
        if host == 'ccgp.gov.cn' or host.endswith('.ccgp.gov.cn'): return '中国政府采购网'
    return PLATFORMS[platform]['name']


def local_time(value):
    if not value:
        return None
    dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt).astimezone(TZ).strftime('%Y-%m-%d %H:%M:%S')


from record_identity import identity_for, substantive, fingerprint


class Records:
    """与原功能共用 SQLite 文件，用独立表保存结构化记录，不修改原 bids 数据。"""
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()

    @contextmanager
    def connect(self):
        with self.lock, closing(sqlite3.connect(self.path, timeout=10)) as con:
            con.row_factory = sqlite3.Row
            con.execute('''CREATE TABLE IF NOT EXISTS competitor_records (
                record_key TEXT PRIMARY KEY, company TEXT NOT NULL, platform TEXT NOT NULL,
                kind TEXT NOT NULL, title TEXT NOT NULL, source_url TEXT NOT NULL,
                fields TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
            con.execute("""CREATE TABLE IF NOT EXISTS competitor_observations (
                observation_key TEXT PRIMARY KEY, record_key TEXT NOT NULL,
                fields TEXT NOT NULL, reason TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
            yield con
            con.commit()

    def save(self, company, platform, record):
        if platform not in PLATFORMS or record['kind'] not in ('company', 'project', 'opening', 'tender') or not _same_site(record['source_url'], platform):
            raise ValueError('记录类型或来源地址不符合指定平台')
        audited = platform == 'ggzy' and record['kind'] in ('tender', 'opening')
        identity, strength = identity_for(record) if audited else (record.get('identity'), None)
        parts = [company, platform, record['kind'], record['title'], record['source_url']]
        if identity:
            parts.append(identity)
        key = hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()
        fields = json.dumps(record['fields'], ensure_ascii=False)
        with self.connect() as con:
            # Obtain a SQLite writer lock before read-modify-write, including other processes.
            con.execute('BEGIN IMMEDIATE')
            existing = con.execute('SELECT * FROM competitor_records WHERE record_key=?', (key,)).fetchone()
            if audited and not existing:
                # Compatibility with previous amount-based keys; keep old primary keys and dates.
                candidates = con.execute('SELECT * FROM competitor_records WHERE company=? AND platform=? AND kind=? AND source_url=?',
                                         (company, platform, record['kind'], record['source_url'])).fetchall()
                matches = [r for r in candidates if identity_for({'title': r['title'], 'fields': json.loads(r['fields'])})[0] == identity]
                if len(matches) > 1:
                    for r in matches:
                        self._observe(con, r['record_key'], record['fields'], 'ambiguous_legacy')
                    return False
                if matches:
                    existing = matches[0]
            if existing:
                key = existing['record_key']
                old = json.loads(existing['fields'])
                if audited and substantive(old) != substantive(record['fields']):
                    if strength != 'source':
                        self._observe(con, key, record['fields'], 'needs_review')
                        return False  # Do not overwrite or count uncertain variants as new projects.
                    self._observe(con, key, old, 'superseded')
                con.execute('UPDATE competitor_records SET fields=?,updated_at=CURRENT_TIMESTAMP WHERE record_key=?', (fields,key))
                return False
            con.execute('INSERT INTO competitor_records(record_key,company,platform,kind,title,source_url,fields) VALUES(?,?,?,?,?,?,?)',
                        (key,company,platform,record['kind'],record['title'],record['source_url'],fields))
            return True

    @staticmethod
    def _observe(con, key, fields, reason):
        obs_key = fingerprint([key, substantive(fields), reason])
        con.execute('INSERT OR IGNORE INTO competitor_observations(observation_key,record_key,fields,reason) VALUES(?,?,?,?)',
                    (obs_key,key,json.dumps(fields,ensure_ascii=False),reason))

    def observations(self, key):
        with self.connect() as con:
            rows = con.execute('SELECT fields,reason,created_at FROM competitor_observations WHERE record_key=? ORDER BY created_at DESC', (key,)).fetchall()
        return [{'fields':json.loads(r['fields']), 'reason':r['reason'], 'collected_at':local_time(r['created_at'])} for r in rows]

    def list(self, companies=None, limit=100):
        where = ' WHERE company IN (' + ','.join('?' for _ in companies) + ')' if companies else ''
        with self.connect() as con:
            rows = con.execute('SELECT * FROM competitor_records' + where + ' ORDER BY created_at DESC,rowid DESC LIMIT ?',
                               (*(companies or []), min(max(int(limit), 1), 500))).fetchall()
        return [{'record_key': r['record_key'], 'observations': self.observations(r['record_key']), 'company': r['company'], 'platform': r['platform'], 'source': record_source_name(r['platform'], r['source_url']),
                 'record_type': r['kind'], 'title': r['title'], 'url': r['source_url'],
                 'fields': json.loads(r['fields']), 'collected_at': local_time(r['created_at'])} for r in rows]

    def counts(self, companies=None):
        where = ' WHERE company IN (' + ','.join('?' for _ in companies) + ')' if companies else ''
        with self.connect() as con:
            row = con.execute("SELECT COUNT(*),COALESCE(SUM(date(created_at,'+8 hours')=date('now','+8 hours')),0) FROM competitor_records" + where,
                              tuple(companies or [])).fetchone()
            review = con.execute("SELECT COUNT(*) FROM competitor_observations o JOIN competitor_records r ON r.record_key=o.record_key WHERE o.reason IN ('needs_review','ambiguous_legacy')" +
                                 (' AND r.company IN (' + ','.join('?' for _ in companies) + ')' if companies else ''), tuple(companies or [])).fetchone()[0]
        return {'total': row[0], 'today_new': row[1], 'review_count': review}


    def summary(self, companies=None):
        where = ' WHERE company IN (' + ','.join('?' for _ in companies) + ')' if companies else ''
        with self.connect() as con:
            rows = con.execute('SELECT company,kind,COUNT(*) AS n,MAX(created_at) AS latest FROM competitor_records' + where + ' GROUP BY company,kind', tuple(companies or [])).fetchall()
            sources = con.execute('SELECT company,platform,source_url,COUNT(*) AS n FROM competitor_records' + where + ' GROUP BY company,platform,source_url', tuple(companies or [])).fetchall()
        groups = {name: {'company': name, 'counts': {k: 0 for k in ('company','project','opening','tender')}, 'sources': [], 'latest': None} for name in (companies or [])}
        for row in rows:
            group = groups.setdefault(row['company'], {'company': row['company'], 'counts': {k: 0 for k in ('company','project','opening','tender')}, 'sources': [], 'latest': None})
            group['counts'][row['kind']] = row['n']
            at = local_time(row['latest'])
            if at and (not group['latest'] or at > group['latest']): group['latest'] = at
        for row in sources:
            name = record_source_name(row['platform'], row['source_url'])
            source = next((s for s in groups[row['company']]['sources'] if s['id'] == row['platform'] and s['name'] == name), None)
            if source: source['records'] += row['n']
            else: groups[row['company']]['sources'].append({'id': row['platform'], 'name': name, 'records': row['n']})
        values = list(groups.values())
        return {'company_count': sum(any(g['counts'].values()) for g in values), 'groups': values,
                'type_counts': {kind: sum(g['counts'][kind] for g in values) for kind in ('company','project','opening','tender')},
                'count_note': '按来源明细统计，不等于跨站去重后的独立项目数；中标结果不等同于完整开标报价'}


class ManualRequired(Exception):
    def __init__(self, message, status='verification_required'):
        super().__init__(message)
        self.status = status


class SiteAccessError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(f'源网站显示 {code} 错误页；不是验证码通过，也不能视为无记录。请先核实官网访问状态。')


def _site_error_code(driver):
    title = str(driver.title or '')
    bodies = _visible(driver, 'body')
    visible_text = title + '\n' + (bodies[0].text[:2000] if bodies else '')
    codes = r'(403|412|429|500|502|503|521|522|523|524)'
    # 只识别明确的错误页文案，不能把项目编号或金额521误判为错误。
    for pattern in (r'(?im)(?:http\s+error|error(?:\s+code)?|status\s+code)\s*[:：]?\s*'+codes+r'\b',
                    r'(?im)^\s*'+codes+r'(?:\s+(?:error|web|service|错误|服务)|\s*$)'):
        match = re.search(pattern, visible_text)
        if match:
            return int(match.group(1))
    return None


def _manual_page_state(driver, timed_out=False):
    try:
        _check_access(driver)
    except SiteAccessError as exc:
        return {'opened': True, 'page_state': 'site_error', 'site_code': exc.code, 'message': str(exc)}
    except ManualRequired as exc:
        return {'opened': True, 'page_state': exc.status, 'message': str(exc)+'。请在窗口内处理后回系统重新查询。'}
    return {'opened': True, 'page_state': 'loading' if timed_out else 'ready',
            'message': '已打开官网首页，但尚未验证登录或企业查询成功。请先在该窗口确认官网可用；完成登录/验证后回系统重新查询。'}


def xpath_text(text):
    if "'" not in text:
        return "'" + text + "'"
    if '"' not in text:
        return '"' + text + '"'
    return 'concat(' + ',"\'",'.join("'" + part + "'" for part in text.split("'")) + ')'


def _visible(driver, selector):
    from selenium.webdriver.common.by import By
    from selenium.common.exceptions import StaleElementReferenceException
    visible = []
    for el in driver.find_elements(By.CSS_SELECTOR, selector):
        try:
            if el.is_displayed():
                visible.append(el)
        except StaleElementReferenceException:
            continue
    return visible


def _check_access(driver):
    code = _site_error_code(driver)
    if code is not None:
        raise SiteAccessError(code)
    title = (driver.title or '').lower()
    if 'environment checking' in title:
        raise ManualRequired('源站正在进行环境验证，请打开验证窗口人工处理')
    if _visible(driver, 'input[type="password"]'):
        raise ManualRequired('源站要求登录，请在独立验证窗口中手动登录', 'login_required')
    if _visible(driver, 'input#vcode, input[name="vcode"], input[name="captcha"], #geetest_captcha'):
        raise ManualRequired('源站要求验证码，请人工完成验证')
    if any(any(word in el.text for word in ('请先登录', '登录后查看', '请登录后查看'))
           for el in _visible(driver, 'body')):
        raise ManualRequired('源站要求登录后查看，请人工登录', 'login_required')
    for el in _visible(driver, '[role="dialog"], .el-message-box, .layui-layer'):
        if any(word in el.text for word in ('验证已过期', '重新验证', '安全验证', '验证码')):
            raise ManualRequired('源站验证已过期或需要验证码，请人工完成验证')
        if '登录' in el.text:
            raise ManualRequired('源站要求登录，请人工登录', 'login_required')


def _company_elements(driver, company):
    from selenium.webdriver.common.by import By
    literal = xpath_text(' '.join(company.split()))
    query = f'//*[normalize-space(.)={literal} and not(.//*[normalize-space(.)={literal}])]'
    candidates = []
    for el in driver.find_elements(By.XPATH, query):
        if el.is_displayed() and el.tag_name.lower() in ('a', 'span', 'button', 'h1', 'h2', 'h3', 'div', 'td'):
            candidates.append(el)
    return candidates


def _same_site(url, platform):
    p = urlsplit(url)
    domains = PLATFORMS[platform].get('domains', (PLATFORMS[platform]['domain'],))
    return p.scheme in ('http', 'https') and any(p.hostname == domain or (p.hostname or '').endswith('.' + domain) for domain in domains)


@contextmanager
def _company_detail(driver, platform, company, wait):
    from selenium.webdriver.common.by import By
    _check_access(driver)
    elements = _company_elements(driver, company)
    if not elements:
        raise ValueError('没有匹配到企业全称')
    parent, old = driver.current_window_handle, set(driver.window_handles)
    original_url = driver.current_url
    try:
        target = elements[0]
        anchors = target.find_elements(By.XPATH, 'ancestor-or-self::a[@href][1]')
        href = anchors[0].get_attribute('href') if anchors else None
        if href and _same_site(href, platform) and href != original_url:
            driver.get(href)
        else:
            target.click()
            wait.until(lambda d: bool(set(d.window_handles)-old) or d.current_url != original_url)
            children = [h for h in driver.window_handles if h not in old]
            if children:
                driver.switch_to.window(children[0])
        wait.until(lambda d: _same_site(d.current_url, platform) and d.current_url != original_url)
        wait.until(lambda d: (_check_access(d) or bool(_company_elements(d, company))))
        yield driver.current_url
    finally:
        for handle in list(driver.window_handles):
            if handle not in old:
                driver.switch_to.window(handle)
                driver.close()
        if parent in driver.window_handles:
            driver.switch_to.window(parent)
            if driver.current_url != original_url:
                driver.back()


def project_records(headers, rows, source_url, company):
    """将原站表头原样保留；合同金额、开工日期不能改名为报价、开标日期。"""
    result = []
    for values in rows:
        fields = {h.strip(): str(v).strip() for h, v in zip(headers, values)
                  if h.strip() and str(v).strip() and not any(x in h for x in ('身份证', '手机号', '联系电话', '姓名', '法定代表人'))}
        title = next((v for h, v in fields.items() if any(x in h for x in ('项目名称', '工程名称', '标段名称'))), None)
        if not title or title in ('暂无数据', '无记录'):
            continue
        kind = 'opening' if any(x in ''.join(fields) for x in ('开标时间', '投标报价', '开标日期')) else (
               'tender' if any(x in ''.join(fields) for x in ('中标金额', '中标单位', '中标人')) else 'project')
        fields['查询企业'] = company
        result.append({'kind': kind, 'title': title, 'source_url': source_url, 'fields': fields})
    return result


def _read_project_tables(driver, source_url, company):
    from selenium.webdriver.common.by import By
    result = []
    seen = set()
    for table in _visible(driver, 'table'):
        bodies = table.find_elements(By.CSS_SELECTOR, 'tbody tr')
        if not bodies:
            continue
        wrappers = table.find_elements(By.XPATH, 'ancestor::*[contains(concat(" ",normalize-space(@class)," ")," el-table ")][1]')
        header_root = wrappers[0] if wrappers else table
        headers = [el.text.strip() for el in header_root.find_elements(By.CSS_SELECTOR, 'thead th')]
        if not any(any(x in h for x in ('项目名称', '工程名称', '标段名称')) for h in headers):
            continue
        rows = [[cell.text for cell in row.find_elements(By.XPATH, './td')] for row in bodies if row.is_displayed()]
        for record in project_records(headers, rows, source_url, company):
            if record['title'] not in seen:
                seen.add(record['title'])
                result.append(record)
    return result[:20]


def _basic_fields(driver, company):
    from selenium.webdriver.common.by import By
    allowed = ('统一社会信用代码', '企业名称', '单位名称', '登记状态', '企业注册属地', '所属省市', '注册资本', '成立日期')
    fields = {'企业名称': company}
    for row in _visible(driver, 'table tr'):
        cells = row.find_elements(By.XPATH, './td|./th')
        texts = [c.text.strip() for c in cells]
        for i in range(len(texts)-1):
            if any(texts[i] == k or texts[i].startswith(k+'(') or texts[i].startswith(k+'（') for k in allowed):
                fields[texts[i]] = texts[i+1][:500]
    return fields


class Sessions:
    """仅管理程序自己的会话；不导出 Cookie、不读取用户原 Chrome 配置。"""
    def __init__(self):
        self.manual = {}

    def create(self, visible=False):
        from selenium import webdriver
        options = webdriver.ChromeOptions()
        if not visible:
            options.add_argument('--headless=new')
        options.add_argument('--window-size=1400,1000')
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(30)
        return driver

    @contextmanager
    def use(self, platform):
        self._discard_closed(platform)
        retained = platform in self.manual
        driver = self.manual.get(platform) if retained else self.create()
        try:
            yield driver
        finally:
            if not retained:
                driver.quit()

    def open_manual(self, platform, company):
        self._discard_closed(platform)
        if platform not in self.manual:
            self.manual[platform] = self.create(visible=True)
        driver = self.manual[platform]
        from selenium.common.exceptions import TimeoutException
        timed_out = False
        try:
            # 人工登录必须从官网入口开始，不能把固定结果页当作验证入口。
            driver.get(PLATFORMS[platform]['url'])
        except TimeoutException:
            timed_out = True
        result = _manual_page_state(driver, timed_out)
        result['home_url'] = PLATFORMS[platform]['url']
        return result

    def _discard_closed(self, platform):
        if platform in self.manual:
            try:
                if self.manual[platform].window_handles:
                    return
            except Exception:
                pass
            driver = self.manual.pop(platform)
            try:
                driver.quit()
            except Exception:
                pass

    def close(self):
        for driver in self.manual.values():
            try:
                driver.quit()
            except Exception:
                pass
        self.manual.clear()


def query_platform(driver, platform, company, timeout=25, stop=None):
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.common.exceptions import TimeoutException, StaleElementReferenceException
    if platform == 'ggzy':
        from public_bidding import query_public_trades
        return query_public_trades(driver, company, _check_access, stop=stop, timeout=timeout)
    url = search_url(platform, company)
    try:
        driver.get(url)
    except TimeoutException:
        _check_access(driver)
        raise
    wait = WebDriverWait(driver, timeout, poll_frequency=0.4,
                         ignored_exceptions=(StaleElementReferenceException,))
    if platform == 'gsxt':
        def search_input(d):
            _check_access(d)
            return next((el for el in _visible(d, 'input[type="text"], input:not([type])')
                         if any(x in (el.get_attribute('placeholder') or '') for x in ('企业', '信用代码', '名称'))), False)
        field = wait.until(search_input)
        field.clear()
        field.send_keys(company)
        field.send_keys(Keys.ENTER)
    try:
        def ready(d):
            if stop and stop.is_set():
                return True
            _check_access(d)
            return _company_elements(d, company)
        wait.until(ready)
    except TimeoutException:
        _check_access(driver)
        empty = [e.text.strip() for e in _visible(driver, '.el-table__empty-text, .layui-none, td')]
        if any(t in ('暂无数据', '无记录', '没有符合条件的记录') for t in empty):
            return [], 'no_results', '页面显示本次查询无匹配记录；不代表企业不存在'
        return [], 'query_failed', '未能确认查询结果已加载；不按无记录处理'
    if stop and stop.is_set():
        return [], 'stopped', '已停止'
    records = []
    try:
        with _company_detail(driver, platform, company, wait) as detail:
            records.append({'kind': 'company', 'title': company, 'source_url': detail, 'fields': _basic_fields(driver, company)})
            for label in PLATFORMS[platform]['tabs']:
                literal = xpath_text(label)
                candidates = [el for el in driver.find_elements(By.XPATH, f'//*[normalize-space(.)={literal} and not(.//*[normalize-space(.)={literal}])]') if el.is_displayed()]
                if not candidates:
                    continue
                candidates[0].click()
                try:
                    use_frame = platform == 'water_credit'
                    def project_ready(d):
                        if use_frame:
                            d.switch_to.default_content()
                        _check_access(d)
                        if use_frame:
                            current_frames = _visible(d, 'iframe#iframeContent')
                            if not current_frames:
                                return False
                            d.switch_to.frame(current_frames[0])
                            _check_access(d)
                        return _read_project_tables(d, detail, company)
                    try:
                        records.extend(wait.until(project_ready))
                    finally:
                        driver.switch_to.default_content()
                except ManualRequired as exc:
                    return records, exc.status, '企业已定位；项目记录需要人工登录或验证'
                except TimeoutException:
                    return records, 'company_only', '已定位企业，未确认有可读取的项目/招投标记录'
                break
    except Exception as exc:
        if not records:
            raise
        status = getattr(exc, 'status', 'site_error' if hasattr(exc, 'code') else 'partial')
        return records, status, f'已保留{len(records)}条记录，后续读取中断（{type(exc).__name__}）'
    return records, 'completed', '已读取当前页公开信息（最多20条项目记录）；项目业绩不等同于开标明细'


class CompetitorService:
    def __init__(self, db_path, log=None):
        self.records = Records(db_path)
        self.sessions = Sessions()
        self.log = log or (lambda message: None)
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.manual_opening = False
        self.state = {'running': False, 'companies': [], 'entries': [], 'completed': 0, 'total': 0}

    def snapshot(self):
        with self.lock:
            return dict(json.loads(json.dumps(self.state, ensure_ascii=False)), manual_opening=self.manual_opening)

    def reserve(self, text, platforms, notice_urls=None):
        from public_bidding import validate_documents
        notice_urls = validate_documents(notice_urls)
        companies, platforms = parse_companies(text), validate_platforms(platforms)
        with self.lock:
            if self.state['running'] or self.manual_opening:
                raise ValueError('已有查询或浏览器窗口操作在执行，请等待或先停止')
            self.stop.clear()
            self.state = {'running': True, 'companies': companies, 'completed': 0, 'total': len(companies)*len(platforms),
                          'entries': [{'company': c, 'platform': p, 'source': PLATFORMS[p]['name'], 'status': 'pending',
                                       'message': '等待查询', 'query_url': search_url(p,c), 'new_count': 0, 'result_count': 0,
                                       'opening_count': 0} for c in companies for p in platforms]}
            for company in companies:
                for url in notice_urls:
                    self.state['entries'].append({'company': company, 'platform': 'ggzy', 'source': '官方公告/附件核验', 'status': 'pending', 'message': '等待核验', 'query_url': url, 'document_url': url, 'new_count': 0, 'result_count': 0, 'opening_count': 0})
            self.state['total'] = len(self.state['entries'])
            return self.snapshot()

    def run(self):
        try:
            for entry in self.state['entries']:
                if self.stop.is_set():
                    break
                with self.lock:
                    entry.update(status='running', message='正在查询源站')
                company, platform = entry['company'], entry['platform']
                try:
                    with self.sessions.use(platform) as driver:
                        if entry.get('document_url'):
                            from public_bidding import query_official_document
                            records, status, message = query_official_document(driver, company, entry['document_url'], _check_access)
                        else:
                            records, status, message = query_platform(driver, platform, company, stop=self.stop)
                    new_count = sum(self.records.save(company, platform, row) for row in records)
                    with self.lock:
                        entry.update(status=status, message=message, result_count=len(records), new_count=new_count,
                                     opening_count=sum(r['kind'] in ('opening','tender') for r in records))
                except SiteAccessError as exc:
                    with self.lock:
                        entry.update(status='site_error', message=str(exc), site_code=exc.code)
                except ManualRequired as exc:
                    with self.lock:
                        entry.update(status=exc.status, message=str(exc))
                except Exception as exc:
                    with self.lock:
                        entry.update(status='query_failed', message='查询失败：'+type(exc).__name__+'；原因尚未确认，请核对官网和错误信息，不应直接视为需要验证码')
                with self.lock:
                    self.state['completed'] += 1
                self.log('[竞品查询] '+PLATFORMS[platform]['name']+' / '+company+'：'+entry['message'])
        finally:
            with self.lock:
                for entry in self.state['entries']:
                    if entry['status'] in ('pending','running'):
                        entry.update(status='stopped', message='已停止')
                self.state['running'] = False

    def open_manual(self, platform, company):
        validate_platforms([platform])
        company = parse_companies(company)[0]
        with self.lock:
            if self.state['running'] or self.manual_opening:
                raise ValueError('请等待当前查询或窗口操作结束，再打开验证窗口')
            self.manual_opening = True
        try:
            # 不能在30秒浏览器导航期间持有状态锁，否则状态接口会一起阻塞。
            result = self.sessions.open_manual(platform, company)
            with self.lock:
                for entry in self.state['entries']:
                    if entry['platform'] == platform and entry['company'] == company:
                        entry['message'] = result['message']
                        if result['page_state'] in ('site_error', 'login_required', 'verification_required'):
                            entry['status'] = result['page_state']
                        if result.get('site_code'):
                            entry['site_code'] = result['site_code']
            return result
        finally:
            with self.lock:
                self.manual_opening = False

    def close(self):
        self.stop.set()
        self.sessions.close()
