"""Collector contracts without launching a browser or using account credentials."""
import asyncio
from copy import deepcopy

import pytest

from app import collector


def search_payload(price=None):
    return {
        "ret": ["SUCCESS::调用成功"],
        "data": {"resultList": [{"data": {"item": {"main": {
            "exContent": {"itemId": "123456789", "title": "RTX 4060 Ti 16GB",
                          "price": price if price is not None else [{"text": "¥"}, {"text": "2,299"}]},
            "clickParam": {"args": {"publishTime": "1760000000000"}},
        }}}}]},
    }


def test_search_retains_identity_publication_and_quote_without_inventing_sale():
    row, = collector.parse_search_payload(search_payload())
    assert row["item_id"] == "123456789"
    assert row["price"] == 2299
    assert row["status"] == "on_sale"
    assert row["published_at"].startswith("2025-")
    assert "sold_price" not in row
    assert row["url"] == "https://www.goofish.com/item?id=123456789"


@pytest.mark.parametrize(("raw", "expected"), [
    ("当前价 ¥1.25万", 12500), ("￥2,300元", 2300), (1999, 1999),
])
def test_search_normalizes_whole_numeric_quotes(raw, expected):
    assert collector.parse_search_payload(search_payload(raw))[0]["price"] == expected


@pytest.mark.parametrize("raw", ["¥100-200", "面议", "NaN", float("inf"), True, "1e309"])
def test_ambiguous_or_invalid_prices_fail_visibly_when_no_listing_remains(raw):
    with pytest.raises(collector.CollectorError):
        collector.parse_search_payload(search_payload(raw))


@pytest.mark.parametrize("payload", [None, [], {"data": None}, {"data": {"resultList": None}},
    {"ret": "FAIL_SYS_USER_VALIDATE", "data": {"resultList": []}},
    {"ret": [], "data": {"resultList": []}},
    {"ret": ["SUCCESS::ok", "FAIL_SYS_USER_VALIDATE"], "data": {"resultList": []}},
])
def test_errors_and_schema_drift_are_never_disguised_as_zero_results(payload):
    with pytest.raises(collector.CollectorError):
        collector.parse_search_payload(payload)


def test_successful_empty_result_list_remains_empty():
    assert collector.parse_search_payload({"ret": ["SUCCESS::ok"], "data": {"resultList": []}}) == []


@pytest.mark.parametrize("broken_card", [None, {}, {"data": None}, {"data": {"item": None}},
    {"data": {"item": {"main": None}}}, {"data": {"item": {"main": {"exContent": None}}}},
])
def test_malformed_cards_do_not_crash_valid_search_batch(broken_card):
    payload = search_payload()
    payload["data"]["resultList"].insert(0, broken_card)
    assert len(collector.parse_search_payload(payload)) == 1


def test_null_click_parameters_and_future_publish_time_are_not_new_publications():
    payload = search_payload()
    main = payload["data"]["resultList"][0]["data"]["item"]["main"]
    main["clickParam"] = None
    assert "published_at" not in collector.parse_search_payload(payload)[0]
    main["clickParam"] = {"args": {"publishTime": "9999999999999"}}
    assert "published_at" not in collector.parse_search_payload(payload)[0]


def test_request_filter_requires_trusted_host_and_real_api_path():
    good = "https://h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/?data=x"
    assert collector._is_search_response(good)
    assert not collector._is_search_response(good.replace("h5api.m.goofish.com", "evil.example"))
    assert not collector._is_search_response("https://h5api.m.goofish.com/other?q=" + collector.SEARCH_API)


class Element:
    def __init__(self, text="", visible=True, eligible=True):
        self.text, self.visible, self.eligible = text, visible, eligible

    async def is_visible(self):
        return self.visible

    async def inner_text(self, **kwargs):
        return self.text

    async def evaluate(self, code):
        return self.eligible


class Elements:
    def __init__(self, items=()):
        self.items = list(items)

    async def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]


class Page:
    def __init__(self, statuses=(), gates=(), login=(), redirect=None, texts=()):
        self.statuses, self.gates, self.login = statuses, gates, login
        self.texts = texts
        self.redirect = redirect
        self.url = "https://www.goofish.com/"
        self.selectors = []
        self.waits = []

    async def goto(self, url, **kwargs):
        self.url = self.redirect or url

    async def wait_for_timeout(self, ms):
        self.waits.append(ms)

    def locator(self, selector):
        self.selectors.append(selector)
        if "punish" in selector:
            return Elements(self.gates)
        if 'iframe[src*="login"]' in selector:
            return Elements(self.login)
        return Elements(self.statuses)

    def get_by_text(self, pattern, exact=False):
        return Elements([element for element in self.texts
                         if (element.text == pattern if isinstance(pattern, str)
                             else pattern.search(element.text))])


OLD = {"item_id": "123456789", "title": "RTX 4060 Ti 16GB", "price": 2299,
       "category": "gpu", "model": "RTX 4060 Ti 16GB"}


def test_hidden_challenge_frame_does_not_block_normal_page():
    assert not asyncio.run(collector._gate(Page(gates=[Element(visible=False)])))
    assert asyncio.run(collector._gate(Page(gates=[Element(visible=False), Element()])))


def test_visible_login_dialog_prevents_cookie_only_login_success():
    assert asyncio.run(collector._login_visible(Page(login=[Element()])))
    assert not asyncio.run(collector._login_visible(Page(login=[Element(visible=False)])))


def test_persistent_login_header_text_does_not_block_an_authenticated_search():
    assert not asyncio.run(collector._login_visible(Page(texts=[Element("登录")])))


def test_ready_page_continues_without_waiting():
    page = Page()
    assert not asyncio.run(collector._wait_ready(page, headless=False))
    assert page.waits == []


def test_headless_gate_stops_immediately_without_manual_wait():
    page = Page(gates=[Element()])
    with pytest.raises(collector.CollectorError, match="无界面采集已停止"):
        asyncio.run(collector._wait_ready(page, headless=True))
    assert page.waits == []


def test_visible_collection_waits_for_human_gate_clearance_without_navigation():
    class HumanVerifiedPage(Page):
        async def wait_for_timeout(self, ms):
            await super().wait_for_timeout(ms)
            self.gates = []  # user completes the challenge in the existing window
    page = HumanVerifiedPage(gates=[Element()])
    assert asyncio.run(collector._wait_ready(page, headless=False))
    assert page.waits == [2000]
    assert page.url == "https://www.goofish.com/"


def test_unresolved_human_verification_has_finite_two_minute_budget():
    page = Page(gates=[Element()])
    with pytest.raises(collector.CollectorError, match="等待超时（120秒）"):
        asyncio.run(collector._wait_ready(page, headless=False))
    assert len(page.waits) == 60
    assert sum(page.waits) == 120_000


def test_observed_illegal_access_screen_blocks_collection_and_login():
    page = Page(texts=[Element("非法访问"), Element("为了保障您的体验，请使用正常浏览器访问闲鱼~")])
    assert asyncio.run(collector._gate(page))
    assert asyncio.run(collector._login_visible(page))
    with pytest.raises(collector.CollectorError, match="平台验证"):
        asyncio.run(collector._detail_observation(page, OLD))


@pytest.mark.parametrize("texts", [
    [Element("非法访问", visible=False), Element("为了保障您的体验，请使用正常浏览器访问闲鱼~", visible=False)],
    [Element("非法访问"), Element("为了保障您的体验，请使用正常浏览器访问闲鱼~", visible=False)],
    [Element("RTX 显卡 非法访问 维修记录"), Element("为了保障您的体验，请使用正常浏览器访问闲鱼~")],
    [Element("非法访问")],
])
def test_hidden_partial_or_listing_word_mentions_do_not_become_access_gate(texts):
    page = Page(texts=texts)
    assert not asyncio.run(collector._gate(page))
    assert not asyncio.run(collector._login_visible(page))


@pytest.mark.parametrize("redirect", [
    "https://www.goofish.com/item?id=987654321",
    "https://www.goofish.com/login",
    "https://example.com/item?id=123456789",
    "https://www.goofish.com/item?id=123456789&id=987654321",
])
def test_redirected_details_cannot_supply_status_for_tracked_item(redirect):
    page = Page(statuses=[Element("商品已售出")], redirect=redirect)
    with pytest.raises(collector.CollectorError):
        asyncio.run(collector._detail_observation(page, OLD))


def test_explicit_own_item_status_records_sale_without_guessing_transaction_price():
    result = asyncio.run(collector._detail_observation(Page(statuses=[Element("商品已售出")]), OLD))
    assert result["status"] == "sold"
    assert result["price"] == 2299
    assert "123456789" in result["evidence"]
    assert "sold_price" not in result


def test_delisted_status_stays_distinct_from_sold():
    result = asyncio.run(collector._detail_observation(Page(statuses=[Element("商品已下架")]), OLD))
    assert result["status"] == "delisted"


@pytest.mark.parametrize("statuses", [[], [Element("商品已售出", visible=False)],
    [Element("商品已售出", eligible=False)], [Element("商品已售出，查看其他卖家商品")],
])
def test_unrelated_hidden_or_unrecognized_status_does_not_become_sale(statuses):
    page = Page(statuses=statuses)
    result = asyncio.run(collector._detail_observation(page, deepcopy(OLD)))
    assert result["status"] == "unknown"
    assert all("button" not in selector for selector in page.selectors)


def test_browser_busy_lock_cannot_be_claimed_twice_and_is_released(monkeypatch):
    monkeypatch.setattr(collector, "_busy", False)
    monkeypatch.setattr(collector, "_login_running", False)
    collector._claim(login=True)
    try:
        assert collector._busy and collector._login_running
        with pytest.raises(collector.CollectorError):
            collector._claim()
    finally:
        collector._release()
    assert not collector._busy and not collector._login_running
