from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.mobile_guidance import MobileGuidance
from app.mobile_runtime import MobileRuntimeError


@pytest.fixture
def mailbox(tmp_path):
    now = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
    image = tmp_path / "screen.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nactual bounded screenshot fixture")
    target = dict(catalog_id=4, task_ids=[1], enabled=True, model="i9-14900KF", query="14900kf", market_scope_id="frozen", catalog_version=1,
                  scope=dict(model="i9-14900KF", storage=None, condition=None, filters={}))
    targets = dict(policy=MobileGuidance.POLICY, targets=[target])

    def evidence(filename):
        if filename != "screen.png":
            raise ValueError("bad filename")
        return image

    result = MobileGuidance(tmp_path, lambda: targets, evidence, now=lambda: now)
    result.data.mkdir()
    request = dict(schema_version="mobile-navigation-v1", request_id="a"*64, screen_hash="b"*64, catalog_id=4,
                   task_ids=[1], target=deepcopy(target), created_at=(now-timedelta(seconds=10)).isoformat(),
                   expires_at=(now+timedelta(seconds=290)).isoformat(),
                   screenshot=dict(filename="screen.png", width=708, height=1562, sha256=hashlib.sha256(image.read_bytes()).hexdigest()),
                   screen_kind="searchEdit", reason="需要导航辅助", ocr=[dict(text="搜索", x=.8, y=.1)])
    envelope = dict(schema_version="mobile-guidance-mailbox-v1", status="pending", request=request, round_count=0)
    result.file.write_text(json.dumps(envelope))
    result.test_envelope, result.test_targets, result.test_image = envelope, targets, image
    result.test_payload = dict(schema_version="mobile-guidance-suggestion-v1", request_id="a"*64, screen_hash="b"*64,
                               catalog_id=4, task_ids=[1], created_at=now.isoformat(), actions=[dict(kind="tap_text", text="搜索")])
    return result


def rewrite(mailbox):
    mailbox.file.write_text(json.dumps(mailbox.test_envelope))


def test_bound_request_evidence_and_atomic_suggestion_no_overwrite(mailbox):
    status = mailbox.status()
    assert status["status"] == "pending" and status["multimodal_model_connected"] is False
    assert status["evidence_url"] == f"/api/mobile/guidance/{'a'*64}/evidence"
    assert mailbox.evidence("a"*64) == mailbox.test_image
    assert mailbox.submit(mailbox.test_payload)["status"] == "suggestion_queued"
    assert json.loads(mailbox.response_file.read_text()) == mailbox.test_payload
    with pytest.raises(MobileRuntimeError, match="覆盖"):
        mailbox.submit(mailbox.test_payload)
    assert not list(mailbox.data.glob("*.tmp"))


@pytest.mark.parametrize("model,query,frozen_query",[
    ("i9-14900","i9-14900","14900"),("i9-14900KF","i9-14900KF","14900kf"),
    ("RTX 4090","RTX 4090","4090"),("RTX 5070 TI","RTX 5070 TI","5070ti"),
    ("B650","B650 主板","B650"),("RTX 4090","RTX 4090 显卡","4090"),
    ("i9-14900","i9-14900处理器","14900"),("DDR5 32GB","DDR5 32GB内存","DDR5 32GB"),
    ("B650","Ｂ６５０　主板","b650")])
def test_exact_node_discovery_query_normalization_is_accepted(mailbox,model,query,frozen_query):
    target=mailbox.test_targets["targets"][0]
    target.update(model=model,query=query)
    request=mailbox.test_envelope["request"]
    request["target"]={**deepcopy(target),"query":frozen_query}
    rewrite(mailbox)
    assert mailbox.submit(mailbox.test_payload)["status"]=="suggestion_queued"


@pytest.mark.parametrize("model,query,frozen_query",[
    ("i9-14900KF","i9-14900KF","14900K"),("i9-14900","14900","14900KF"),
    ("RTX 4090","RTX 4090","4090TI"),("RTX 4090","RTX 4090华硕","4090"),
    ("RTX 4090","RTX 4090 24GB","4090"),("B650","B650M 主板","B650"),
    ("B650","B650 主板白色","B650"),("i9-14900","14900处理器","14900"),
    ("Ryzen 7800X3D","R7 7800X3D","7800X3D"),("B650","B650 主板",None)])
def test_query_equivalence_never_uses_family_substrings_or_erases_extra_scope(mailbox,model,query,frozen_query):
    target=mailbox.test_targets["targets"][0]
    target.update(model=model,query=query)
    mailbox.test_envelope["request"]["target"]={**deepcopy(target),"query":frozen_query}
    rewrite(mailbox)
    with pytest.raises(MobileRuntimeError,match="型号或查询"):
        mailbox.submit(mailbox.test_payload)
    assert not mailbox.response_file.exists()


@pytest.mark.parametrize("ocr",[[],[dict(text="请搜索")],[dict(text="搜索"),dict(text="搜　索")]])
def test_tap_text_requires_one_exact_normalized_original_ocr_anchor(mailbox,ocr):
    mailbox.test_envelope["request"]["ocr"]=ocr
    rewrite(mailbox)
    with pytest.raises(MobileRuntimeError,match="唯一精确"):
        mailbox.submit(mailbox.test_payload)
    assert not mailbox.response_file.exists()


def test_ocr_anchor_matching_nfkc_ignores_whitespace_without_substrings(mailbox):
    mailbox.test_envelope["request"]["ocr"]=[dict(text=" 搜　索 ",x=.8,y=.1)]
    rewrite(mailbox)
    assert mailbox.submit(mailbox.test_payload)["ok"]


@pytest.mark.parametrize("screen_kind",["XianyuHome","searchResults","market","unknown","popup"])
def test_query_input_is_rejected_unless_original_screen_is_search_edit(mailbox,screen_kind):
    mailbox.test_envelope["request"]["screen_kind"]=screen_kind
    rewrite(mailbox)
    with pytest.raises(MobileRuntimeError,match="搜索编辑"):
        mailbox.submit({**mailbox.test_payload,"actions":[dict(kind="type_target_query")]})
    assert not mailbox.response_file.exists()


def test_search_edit_allows_only_current_target_query_action(mailbox):
    assert mailbox.submit({**mailbox.test_payload,"actions":[dict(kind="type_target_query")]})["ok"]


@pytest.mark.parametrize("change", ["removed", "disabled", "task_changed", "query_changed", "model_changed", "wrong_policy", "scope_changed", "version_changed", "scope_id_changed"])
def test_revoked_targets_cannot_submit_or_expose_evidence(mailbox, change):
    target = mailbox.test_targets["targets"][0]
    if change == "removed":
        mailbox.test_targets["targets"].clear()
    elif change == "disabled":
        target["enabled"] = False
    elif change == "task_changed":
        target["task_ids"] = [2]
    elif change == "query_changed":
        target["query"] = "other"
    elif change == "model_changed":
        target["model"] = "other"
    elif change == "scope_changed":
        target["scope"]["condition"] = "other condition"
    elif change == "version_changed":
        target["catalog_version"] = 2
    elif change == "scope_id_changed":
        target["market_scope_id"] = "changed"
    else:
        mailbox.test_targets["policy"] = "all"
    with pytest.raises(MobileRuntimeError):
        mailbox.submit(mailbox.test_payload)
    with pytest.raises(MobileRuntimeError):
        mailbox.evidence("a"*64)
    assert mailbox.status()["status"] == "unavailable" and not mailbox.response_file.exists()


@pytest.mark.parametrize("actions", [[], [dict(kind="click", x=10, y=20)], [dict(kind="tap_text", text="立即购买")],
                                    [dict(kind="tap_text", text="搜索", x=.3)], [dict(kind="type_target_query", text="other")],
                                    [dict(kind="tap_text", text="搜索")]*4, [None], [dict(kind="run", command="shell")]])
def test_arbitrary_ui_and_business_actions_never_queued(mailbox, actions):
    payload = {**mailbox.test_payload, "actions": actions}
    with pytest.raises(MobileRuntimeError):
        mailbox.submit(payload)
    assert not mailbox.response_file.exists()


@pytest.mark.parametrize("field,value", [("request_id", "c"*64), ("screen_hash", "c"*64), ("catalog_id", True),
                                         ("task_ids", [2]), ("task_ids", [1, 1]), ("created_at", "2020-01-01T00:00:00Z"),
                                         ("created_at", "2026-10-30T12:00:10Z"), ("created_at", "2026-10-30T12:00:00"),
                                         ("extra", "unexpected")])
def test_suggestion_is_bound_to_request_and_time(mailbox, field, value):
    with pytest.raises(MobileRuntimeError):
        mailbox.submit({**mailbox.test_payload, field: value})
    assert not mailbox.response_file.exists()


def test_expired_request_round_limit_and_replaced_screenshot_fail_closed(mailbox):
    mailbox.test_envelope["request"]["expires_at"] = "2026-10-30T11:59:59Z"
    rewrite(mailbox)
    with pytest.raises(MobileRuntimeError, match="过期"):
        mailbox.submit(mailbox.test_payload)
    mailbox.test_envelope["request"]["expires_at"] = "2026-10-30T12:04:50Z"
    mailbox.test_envelope["round_count"] = 2
    rewrite(mailbox)
    with pytest.raises(MobileRuntimeError, match="两轮"):
        mailbox.submit(mailbox.test_payload)
    mailbox.test_envelope["round_count"] = 0
    rewrite(mailbox)
    mailbox.test_image.write_bytes(b"changed")
    with pytest.raises(MobileRuntimeError, match="截图内容"):
        mailbox.submit(mailbox.test_payload)


def test_safe_read_paths_and_no_request_do_not_create_files(mailbox, tmp_path):
    mailbox.file.unlink()
    assert mailbox.status()["status"] == "none"
    mailbox.file.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(MobileRuntimeError, match="符号链接"):
        mailbox.status()
    assert not mailbox.response_file.exists()


def test_guidance_api_local_json_origin_and_pause_guards(mailbox, tmp_path):
    app = create_app(tmp_path / "guidance-api.sqlite3")
    app.state.mobile_guidance = mailbox
    with TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000)) as client:
        assert client.get("/api/mobile/guidance").json()["status"] == "pending"
        assert client.get(f"/api/mobile/guidance/{'a'*64}/evidence").content == mailbox.test_image.read_bytes()
        assert client.post("/api/mobile/guidance", json=mailbox.test_payload, headers={"Origin": "https://foreign.test"}).status_code == 403
        assert client.post("/api/mobile/guidance", content="").status_code == 415
        app.state.workflow.pause_mobile("test explicit pause")
        assert client.post("/api/mobile/guidance", json=mailbox.test_payload).status_code == 409
        assert not mailbox.response_file.exists()
        app.state.workflow.resume_mobile("test explicit resume")
        assert client.post("/api/mobile/guidance", json=mailbox.test_payload).status_code == 200
    with TestClient(app, base_url="http://127.0.0.1:8765", client=("192.0.2.1", 50000)) as client:
        assert client.get("/api/mobile/guidance").status_code == 403
        assert client.get(f"/api/mobile/guidance/{'a'*64}/evidence").status_code == 403


def test_home_search_guidance_requires_current_observed_anchor(mailbox):
    request = mailbox.test_envelope["request"]
    request["screen_kind"] = "XianyuHome"
    request["ocr"] = home_ocr()
    rewrite(mailbox)
    payload = {**mailbox.test_payload, "actions": [dict(kind="tap_home_search", text="活力28消毒液")]}
    assert mailbox.submit(payload)["ok"]
    mailbox.response_file.unlink()
    for change in ("different_text", "wrong_region", "wrong_screen", "duplicate", "sensitive"):
        request["screen_kind"] = "XianyuHome"
        request["ocr"] = home_ocr()
        if change == "different_text":
            request["ocr"][0]["text"] = "another"
        elif change == "wrong_region":
            request["ocr"][0]["y"] = .6
        elif change == "wrong_screen":
            request["screen_kind"] = "unknown"
        elif change == "duplicate":
            request["ocr"] *= 2
        else:
            request["ocr"].append(dict(text="确认付款", x=.5, y=.1))
        rewrite(mailbox)
        with pytest.raises(MobileRuntimeError):
            mailbox.submit(payload)
        assert not mailbox.response_file.exists()


def home_ocr(text="活力28消毒液",x=.12,y=.175):
    return [dict(text=text,x=x,y=y),dict(text="首页集市 圈子境拍中",x=.08,y=.11),
            dict(text="回收",x=.8,y=.11),dict(text="首页",x=.1,y=.95),
            dict(text="消息",x=.7,y=.95),dict(text="我",x=.9,y=.95)]


def home_request(mailbox,lines=None,text="活力28消毒液"):
    mailbox.test_envelope["request"].update(screen_kind="XianyuHome",ocr=home_ocr() if lines is None else lines)
    rewrite(mailbox)
    return {**mailbox.test_payload,"actions":[dict(kind="tap_home_search",text=text)]}


@pytest.mark.parametrize("x,y",[(.03,.14),(.8,.25),(.03,.25),(.8,.14)])
def test_home_anchor_region_edges_are_inclusive_like_node(mailbox,x,y):
    assert mailbox.submit(home_request(mailbox,home_ocr(x=x,y=y)))["ok"]


def test_home_with_merged_top_chrome_and_short_my_label_is_trusted(mailbox):
    # The real header was one Vision line; '我' was the actual bottom OCR.
    lines=home_ocr(text="ＭＬＢ 帽子")
    assert mailbox.submit(home_request(mailbox,lines,text="mlb帽子"))["ok"]


@pytest.mark.parametrize("change",["no_top","only_two_top_tokens","repeated_top_token","no_bottom_home","no_message","no_my","bottom_in_feed","top_at_boundary"])
def test_home_label_alone_without_independent_chrome_is_not_trusted(mailbox,change):
    lines=home_ocr()
    if change=="no_top":lines=[line for line in lines if line["y"]>.14]
    elif change=="only_two_top_tokens":lines[1]["text"]="首页集市";lines[2]["text"]="别的"
    elif change=="repeated_top_token":lines[1]["text"]="首页首页首页";lines[2]["text"]="首页"
    elif change=="no_bottom_home":lines[3]["text"]="别的"
    elif change=="no_message":lines[4]["text"]="别的"
    elif change=="no_my":lines[5]["text"]="别的"
    elif change=="bottom_in_feed":
        for line in lines[3:]:line["y"]=.9
    else:
        lines[1]["y"]=.07;lines[2]["y"]=.18
    with pytest.raises(MobileRuntimeError):mailbox.submit(home_request(mailbox,lines))
    assert not mailbox.response_file.exists()


def test_duplicate_search_text_in_feed_invalidates_whole_screenshot_anchor(mailbox):
    lines=home_ocr();lines.append(dict(text="活力 28 消毒液",x=.2,y=.6))
    with pytest.raises(MobileRuntimeError):mailbox.submit(home_request(mailbox,lines))
    assert not mailbox.response_file.exists()


@pytest.mark.parametrize("x,y",[(.029,.17),(.801,.17),(.1,.139),(.1,.251)])
def test_home_search_pill_cannot_escape_bounded_region(mailbox,x,y):
    with pytest.raises(MobileRuntimeError):mailbox.submit(home_request(mailbox,home_ocr(x=x,y=y)))


def test_feed_photo_payment_words_do_not_invalidate_verified_home_chrome(mailbox):
    lines=home_ocr();lines.append(dict(text="支付方式：微信",x=.2,y=.61))
    assert mailbox.submit(home_request(mailbox,lines))["ok"]


@pytest.mark.parametrize("y",[.1,.95])
def test_sensitive_top_or_bottom_chrome_prevents_home_navigation(mailbox,y):
    lines=home_ocr();lines.append(dict(text="权限申请允许访问",x=.2,y=y))
    with pytest.raises(MobileRuntimeError):mailbox.submit(home_request(mailbox,lines))
    assert not mailbox.response_file.exists()
