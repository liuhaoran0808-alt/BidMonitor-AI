"""四库适配回归测试：全部使用本地假驱动，不访问网站或数据库。"""
import threading
import unittest
from unittest.mock import Mock, patch

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By

from src.crawler.jzsc import (
    _ROWS, crawl_company_results, is_company_search_url,
)
from src.crawler.selenium_crawler import SeleniumCrawler


SEARCH = "https://jzsc.mohurd.gov.cn/data/company?complexname=浪潮软件股份有限公司"
DETAIL = "https://jzsc.mohurd.gov.cn/data/company/detail?id=002503072041900985"


class ImmediateWait:
    def __init__(self, driver, *args, **kwargs):
        self.driver = driver

    def until(self, predicate):
        for _ in range(3):
            value = predicate(self.driver)
            if value:
                return value
        raise TimeoutException("test-only wait timeout")


class Element:
    def __init__(self, text="", children=None):
        self.text = text
        self.children = children or []

    def is_displayed(self):
        return True

    def find_elements(self, by, selector):
        return self.children


class Link(Element):
    def __init__(self, driver, name, url):
        super().__init__(name)
        self.driver = driver
        self.url = url

    def click(self):
        driver = self.driver
        driver.click_count += 1
        if driver.mode == "error":
            raise RuntimeError("test-only click failure")
        target = "https://jzsc.mohurd.gov.cn/login" if driver.mode == "login" else self.url
        if driver.mode in ("popup", "login"):
            driver.pages["new-detail"] = target
        else:
            driver.pages[driver.handle] = target


class Driver:
    def __init__(self, mode="popup", empty=False, loading=False, companies=None):
        self.mode = mode
        self.empty = empty
        self.loading = loading
        self.pages = {"main": SEARCH, "existing": "https://example.org/already-open"}
        self.handle = "main"
        self.switch_to = Mock()
        self.switch_to.window.side_effect = self.switch
        self.closed = []
        self.click_count = 0
        self.companies = companies if companies is not None else [
            ("浪潮软件股份有限公司", "913700004941904564", "山东省-泰安市", DETAIL)]

    @property
    def current_url(self):
        return self.pages[self.handle]

    @property
    def current_window_handle(self):
        return self.handle

    @property
    def window_handles(self):
        return list(self.pages)

    def switch(self, handle):
        self.handle = handle

    def close(self):
        self.closed.append(self.handle)
        del self.pages[self.handle]

    def back(self):
        self.pages[self.handle] = SEARCH

    def find_elements(self, by, selector):
        if selector == _ROWS:
            if self.empty or self.loading or self.current_url != SEARCH:
                return []
            return [Element(children=[
                Element("1"), Element(code), Element(children=[Link(self, name, url)]),
                Element("not collected"), Element(region)])
                for name, code, region, url in self.companies]
        if selector == ".el-table__empty-text" and self.empty:
            return [Element("暂无数据")]
        if selector == ".el-loading-mask" and self.loading:
            return [Element()]
        return []


class JzscTests(unittest.TestCase):
    def setUp(self):
        self.wait_patch = patch("src.crawler.jzsc.WebDriverWait", ImmediateWait)
        self.wait_patch.start()
        self.addCleanup(self.wait_patch.stop)
        self.logger = Mock()

    def run_adapter(self, driver, **kwargs):
        return crawl_company_results(driver, SEARCH, "四库测试", logger=self.logger, **kwargs)

    def test_url_guard_excludes_other_hosts_and_details(self):
        self.assertTrue(is_company_search_url(SEARCH))
        self.assertTrue(is_company_search_url("https://jzsc.mohurd.gov.cn/data/company/"))
        for url in (DETAIL, "https://example.org/data/company", "https://jzsc.mohurd.gov.cn.evil.test/data/company"):
            self.assertFalse(is_company_search_url(url))

    def test_real_span_row_shape_returns_company_detail(self):
        driver = Driver()
        bids = self.run_adapter(driver)
        self.assertEqual(len(bids), 1)
        self.assertEqual(bids[0].title, "浪潮软件股份有限公司")
        self.assertEqual(bids[0].url, DETAIL)
        self.assertIn("913700004941904564", bids[0].content)
        self.assertEqual(bids[0].publish_date, "")
        self.assertEqual(driver.closed, ["new-detail"])
        self.assertEqual(driver.window_handles, ["main", "existing"])
        self.assertEqual(driver.current_url, SEARCH)

    def test_same_tab_navigation_collects_multiple_rows(self):
        other = "https://jzsc.mohurd.gov.cn/data/company/detail?id=test-fixture-only"
        driver = Driver(mode="same", companies=[
            ("测试企业甲", "FIXTURE-A", "测试属地", DETAIL),
            ("测试企业乙", "FIXTURE-B", "测试属地", other)])
        bids = self.run_adapter(driver)
        self.assertEqual([b.url for b in bids], [DETAIL, other])
        self.assertEqual(driver.closed, [])
        self.assertIn("existing", driver.window_handles)

    def test_explicit_no_results_is_empty_list(self):
        self.assertEqual(self.run_adapter(Driver(empty=True)), [])

    def test_still_loading_is_failure_not_empty_results(self):
        self.assertIsNone(self.run_adapter(Driver(loading=True)))

    def test_login_redirect_is_failure_and_closes_only_child(self):
        driver = Driver(mode="login")
        self.assertIsNone(self.run_adapter(driver))
        self.assertEqual(driver.closed, ["new-detail"])
        self.assertEqual(driver.current_window_handle, "main")

    def test_click_failure_is_not_silently_reported_as_zero(self):
        self.assertIsNone(self.run_adapter(Driver(mode="error")))

    def test_stop_before_start_does_not_click(self):
        driver = Driver()
        event = threading.Event()
        event.set()
        self.assertEqual(self.run_adapter(driver, stop_event=event), [])
        self.assertEqual(driver.click_count, 0)

    def test_missing_query_refuses_to_crawl_all_companies(self):
        driver = Driver()
        self.assertIsNone(crawl_company_results(
            driver, "https://jzsc.mohurd.gov.cn/data/company", "测试", logger=self.logger))
        self.assertEqual(driver.click_count, 0)

    def test_duplicate_rows_are_not_clicked_twice(self):
        row = ("测试企业甲", "FIXTURE-A", "测试属地", DETAIL)
        driver = Driver(companies=[row, row])
        self.assertEqual(len(self.run_adapter(driver)), 1)
        self.assertEqual(driver.click_count, 1)

    def test_result_limit_is_applied(self):
        driver = Driver(companies=[
            ("测试企业甲", "FIXTURE-A", "测试属地", DETAIL),
            ("测试企业乙", "FIXTURE-B", "测试属地", DETAIL + "2")])
        self.assertEqual(len(self.run_adapter(driver, max_results=1)), 1)
        self.assertEqual(driver.click_count, 1)
        self.assertTrue(self.logger.warning.called)

    def test_selenium_crawl_dispatches_jzsc_without_generic_parser(self):
        crawler = SeleniumCrawler({}, "四库测试", SEARCH)
        crawler.driver = Driver()
        crawler.fetch = Mock(return_value="<body>SPA shell</body>")
        crawler.parse = Mock(side_effect=AssertionError("must not use a-only parser"))
        bids = crawler.crawl()
        self.assertEqual(bids[0].url, DETAIL)
        crawler.parse.assert_not_called()

    def test_other_sites_keep_existing_anchor_parser(self):
        crawler = SeleniumCrawler({}, "原有站点", "https://example.org/list")
        crawler.fetch = Mock(return_value='<a href="/notice/1">测试采购公告</a>')
        bids = crawler.crawl()
        self.assertEqual(len(bids), 1)
        self.assertEqual(bids[0].url, "https://example.org/notice/1")


if __name__ == "__main__":
    unittest.main()
