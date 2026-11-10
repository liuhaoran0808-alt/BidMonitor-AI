"""四库企业结果页适配：读取可见表格，通过正常点击获取详情链接。

仅处理含 complexname 的固定搜索结果页；不处理登录、验证码或分页。
"""
import logging
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from .base import BidInfo


_HOST = "jzsc.mohurd.gov.cn"
_ROWS = "table.el-table__body tbody tr.el-table__row"


def is_company_search_url(url):
    parts = urlsplit(url)
    return (parts.scheme in ("http", "https") and parts.hostname == _HOST
            and parts.path.rstrip("/") == "/data/company")


def _is_detail_url(url):
    parts = urlsplit(url)
    return (parts.scheme in ("http", "https") and parts.hostname == _HOST
            and parts.path.rstrip("/") == "/data/company/detail"
            and bool(parse_qs(parts.query).get("id", [""])[0].strip()))


@dataclass(frozen=True)
class _Company:
    name: str
    credit_code: str
    region: str


def _visible_companies(driver):
    """字段位置来自实测表格：序号、信用代码、企业名、法人、属地。"""
    found = []
    for row in driver.find_elements(By.CSS_SELECTOR, _ROWS):
        if not row.is_displayed():
            continue
        cells = row.find_elements(By.TAG_NAME, "td")
        if len(cells) < 5:
            continue
        for link in cells[2].find_elements(By.CSS_SELECTOR, "span.link"):
            name = link.text.strip()
            if name and link.is_displayed():
                company = _Company(name, cells[1].text.strip(), cells[4].text.strip())
                found.append((company, link))
                break
    return found


def _find_link(driver, company):
    for current, link in _visible_companies(driver):
        if current == company:
            return link
    return False


def _explicitly_empty(driver):
    if any(el.is_displayed() for el in driver.find_elements(
            By.CSS_SELECTOR, ".el-loading-mask")):
        return False
    return any(el.is_displayed() and "暂无数据" in el.text
               for el in driver.find_elements(By.CSS_SELECTOR, ".el-table__empty-text"))


def _detail_link(driver, company, wait, stop_event, restore_list, logger):
    parent = driver.current_window_handle
    original_handles = set(driver.window_handles)
    stopped = lambda: stop_event is not None and stop_event.is_set()
    try:
        link = wait.until(lambda d: stopped() or _find_link(d, company))
        if stopped():
            return None
        link.click()
        # 官网实测为新标签；兼容同一标签导航，不读取 Vue 内部状态。
        wait.until(lambda d: stopped() or
                   bool(set(d.window_handles) - original_handles) or
                   _is_detail_url(d.current_url))
        if stopped():
            return None
        children = [h for h in driver.window_handles if h not in original_handles]
        if children:
            driver.switch_to.window(children[0])
        wait.until(lambda d: stopped() or _is_detail_url(d.current_url))
        return None if stopped() else driver.current_url
    finally:
        # 只关闭本次点击产生的标签，不能关闭调用方已有的标签。
        for handle in list(driver.window_handles):
            if handle not in original_handles:
                try:
                    driver.switch_to.window(handle)
                    driver.close()
                except Exception as exc:
                    logger.warning("[四库] 清理详情标签失败: %s", exc)
        if parent in driver.window_handles:
            driver.switch_to.window(parent)
            if restore_list and not stopped() and _is_detail_url(driver.current_url):
                driver.back()
                wait.until(lambda d: stopped() or _find_link(d, company))


def crawl_company_results(driver, search_url, source, timeout=30,
                          stop_event=None, logger=None, max_results=10):
    """抓取已打开的企业列表；失败返回 None，明确无数据才返回 []。"""
    logger = logger or logging.getLogger(__name__)
    stopped = lambda: stop_event is not None and stop_event.is_set()
    if stopped():
        return []
    keyword = parse_qs(urlsplit(search_url).query).get("complexname", [""])[0].strip()
    if not is_company_search_url(search_url) or not keyword:
        logger.error("[四库] 请使用含 complexname 查询条件的企业结果页地址")
        return None
    wait = WebDriverWait(driver, max(1, float(timeout)), poll_frequency=0.25)
    try:
        entries = wait.until(lambda d: stopped() or _visible_companies(d))
    except TimeoutException:
        if stopped():
            return []
        if _explicitly_empty(driver):
            logger.info("[四库] 查询结果明确显示暂无数据")
            return []
        logger.error("[四库] 企业结果表未加载，可能超时或需要人工验证；不按无记录处理")
        return None
    if stopped():
        return []
    companies = list(dict.fromkeys(company for company, _ in entries))
    limit = max(1, min(int(max_results), 20))
    if len(companies) > limit:
        logger.warning("[四库] 当前页 %s 家企业，本次仅采集前 %s 家", len(companies), limit)
    companies = companies[:limit]
    bids = []
    for index, company in enumerate(companies):
        if stopped():
            break
        try:
            detail_url = _detail_link(driver, company, wait, stop_event,
                                      index < len(companies) - 1, logger)
            if detail_url:
                bids.append(BidInfo(
                    title=company.name, url=detail_url, publish_date="", source=source,
                    content=(f"企业名称：{company.name}\n统一社会信用代码：{company.credit_code}"
                             f"\n企业注册属地：{company.region}")))
        except Exception as exc:
            logger.warning("[四库] 未取得企业 %s 的详情链接: %s", company.name, exc)
    logger.info("[四库] 当前页识别 %s 家企业，取得 %s 条详情链接", len(companies), len(bids))
    return bids if bids or stopped() else None
