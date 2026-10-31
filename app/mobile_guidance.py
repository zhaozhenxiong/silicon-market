"""Local, bounded navigation suggestions for the native phone worker.

This mailbox does not call a model or drive a device. It binds an offline AI
suggestion to one unexpired screenshot and the current enabled monitor tasks.
The worker rechecks the screen and authorization before every input.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from .mobile_runtime import MobileRuntimeError


def _identity(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or ""))).lower()


def normalize_discovery_query(catalog):
    """Mirror normalizeDiscoveryQuery in mobile_scope_discovery.mjs.

    Category suffixes are removable only after a complete canonical ASCII
    model. Additional model suffixes, specifications and brands stay intact.
    """
    model = str(catalog.get("model") or "").strip()
    query = unicodedata.normalize("NFKC", str(catalog.get("query") or "")).strip()
    compact, base = _identity(query), _identity(model)
    suffix = compact[len(base):] if compact.startswith(base) else None
    ascii_model = bool(model and re.fullmatch(r"[\x20-\x7e]+", model) and re.search(r"[A-Za-z0-9]", model))
    chosen = model if ascii_model and suffix in {"主板", "显卡", "处理器", "内存"} else query or model
    chosen = unicodedata.normalize("NFKC", chosen).strip()
    chosen = re.sub(r"^i9\s*[- ]\s*(?=[0-9])", "", chosen, flags=re.I)
    return re.sub(r"^RTX\s*", "", chosen, flags=re.I).strip()


def _coordinate(line, name):
    value = line.get(name)
    return value if type(value) in {int, float} and math.isfinite(value) else float("nan")


def trusted_xianyu_home(lines):
    """Mirror trustedXianyuHome's independent top and bottom chrome proof."""
    headers = [line for line in lines if .07 < _coordinate(line, "y") < .18]
    tokens = [word for word in ("首页", "集市", "圈子", "回收")
              if any(word in _identity(line.get("text")) for line in headers)]
    bottom = [line for line in lines if _coordinate(line, "y") > .91]
    return (len(tokens) >= 3
            and any(_identity(line.get("text")) in {"闲鱼", "首页"} for line in bottom)
            and any(_identity(line.get("text")) == "消息" for line in bottom)
            and any(_identity(line.get("text")) in {"我", "我的"} for line in bottom))


class MobileGuidance:
    POLICY = "enabled_live_monitor_tasks_only"
    TEXTS = {"首页", "搜索", "行情", "返回", "关闭", "取消"}
    SENSITIVE = re.compile(r"用户协议|隐私政策|服务协议|授权|权限|允许.*访问|登录|登陆|验证码|密码|支付|付款|支付宝|购买|下单")
    RESPONSE_FIELDS = {"schema_version", "request_id", "screen_hash", "catalog_id", "task_ids", "created_at", "actions"}

    def __init__(self, root, target_provider, evidence_provider, now=None):
        self.data = Path(root) / "data"
        self.file = self.data / "native_mobile_guidance.json"
        self.response_file = self.data / "native_mobile_guidance_response.json"
        self.target_provider = target_provider
        self.evidence_provider = evidence_provider
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.lock = threading.RLock()

    @staticmethod
    def _time(value):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if result.tzinfo is None:
                raise ValueError
            return result
        except (AttributeError, TypeError, ValueError):
            raise MobileRuntimeError("导航请求时间格式无效") from None

    @staticmethod
    def _ids(value):
        if not isinstance(value, list) or not value or any(type(x) is not int or x <= 0 for x in value) or len(value) != len(set(value)):
            raise MobileRuntimeError("导航建议须绑定明确监控任务集合")
        return sorted(value)

    def _read(self):
        if self.data.is_symlink() or self.file.is_symlink():
            raise MobileRuntimeError("导航资料路径不可使用符号链接")
        try:
            if not self.file.is_file():
                return None
            if self.file.stat().st_size > 2_000_000:
                raise MobileRuntimeError("导航请求超过允许大小")
            value = json.loads(self.file.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("schema_version") != "mobile-guidance-mailbox-v1":
                raise MobileRuntimeError("导航请求格式无效")
            return value
        except (OSError, UnicodeError, ValueError) as exc:
            if isinstance(exc, MobileRuntimeError):
                raise
            raise MobileRuntimeError("无法读取本机导航请求") from None

    def _validate(self, envelope):
        if not envelope or envelope.get("status") != "pending":
            raise MobileRuntimeError("当前没有待处理导航请求")
        request = envelope.get("request", {})
        if not isinstance(request, dict) or request.get("schema_version") != "mobile-navigation-v1":
            raise MobileRuntimeError("导航请求格式无效")
        for key in ("request_id", "screen_hash"):
            if not isinstance(request.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", request[key]):
                raise MobileRuntimeError("导航请求缺少有效屏幕指纹")
        created, expires, now = self._time(request.get("created_at")), self._time(request.get("expires_at")), self.now()
        if created > now or expires <= now or not 0 < (expires-created).total_seconds() <= 300:
            raise MobileRuntimeError("导航请求已过期或时间范围无效")
        rounds = envelope.get("round_count", 0)
        if type(rounds) is not int or not 0 <= rounds < 2:
            raise MobileRuntimeError("此目标导航建议已达到两轮上限")
        snapshot = self.target_provider()
        if not isinstance(snapshot, dict) or snapshot.get("policy") != self.POLICY:
            raise MobileRuntimeError("未确认已启用监控任务白名单")
        target = next((x for x in snapshot.get("targets", []) if x.get("catalog_id") == request.get("catalog_id") and x.get("enabled") is True), None)
        if target is None or type(request.get("catalog_id")) is not int:
            raise MobileRuntimeError("导航目标已不属于已启用监控任务")
        if self._ids(target.get("task_ids")) != self._ids(request.get("task_ids")):
            raise MobileRuntimeError("导航目标的监控任务集合已变化")
        frozen = request.get("target", {})
        if (not isinstance(frozen, dict) or frozen.get("model") != target.get("model")
                or not isinstance(frozen.get("query"), str) or not frozen["query"].strip()
                or not isinstance(target.get("query"), str) or not target["query"].strip()
                or _identity(normalize_discovery_query(frozen)) != _identity(normalize_discovery_query(target))):
            raise MobileRuntimeError("导航请求的型号或查询已变化")
        for key in ("market_scope_id", "catalog_version"):
            if target.get(key) is not None and frozen.get(key) != target[key]:
                raise MobileRuntimeError("导航请求的经济范围或目录版本已变化")
        if isinstance(target.get("scope"), dict):
            old_scope = frozen.get("scope")
            if not isinstance(old_scope, dict) or any(old_scope.get(key) != target["scope"].get(key) for key in ("model", "storage", "condition", "filters")):
                raise MobileRuntimeError("导航请求的规格、成色或筛选范围已变化")
        return request

    def _evidence(self, request):
        shot = request.get("screenshot", {})
        if not isinstance(shot, dict) or not isinstance(shot.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", shot["sha256"]):
            raise MobileRuntimeError("导航请求尚未绑定实际截图SHA256")
        try:
            path = self.evidence_provider(shot.get("filename"))
            if hashlib.sha256(path.read_bytes()).hexdigest() != shot["sha256"]:
                raise MobileRuntimeError("导航截图内容已变化")
            return path
        except (OSError, ValueError) as exc:
            if isinstance(exc, MobileRuntimeError):
                raise
            raise MobileRuntimeError("导航截图证据不可用") from None

    def status(self):
        envelope = self._read()
        if not envelope:
            return dict(status="none", message="没有待处理导航请求", multimodal_model_connected=False)
        result = {key: envelope.get(key) for key in ("status", "round_count", "updated_at", "message", "result")}
        result["multimodal_model_connected"] = False
        if envelope.get("status") != "pending":
            return result
        try:
            request = self._validate(envelope)
            self._evidence(request)
        except MobileRuntimeError as exc:
            return {**result, "status": "unavailable", "message": str(exc)}
        # Bounded source evidence is data for the reviewing assistant, not code.
        result["request"] = {key: request.get(key) for key in ("schema_version", "request_id", "screen_hash", "catalog_id", "task_ids", "created_at", "expires_at", "target", "screen_kind", "reason", "screenshot")}
        result["request"]["ocr"] = request.get("ocr", [])[:100]
        result["evidence_url"] = f"/api/mobile/guidance/{request['request_id']}/evidence"
        result["response_pending"] = self.response_file.exists()
        return result

    def evidence(self, request_id):
        request = self._validate(self._read())
        if request_id != request["request_id"]:
            raise MobileRuntimeError("导航截图请求已失效")
        return self._evidence(request)

    def submit(self, payload):
        with self.lock:
            envelope = self._read()
            request = self._validate(envelope)
            self._evidence(request)
            if not isinstance(payload, dict) or set(payload) != self.RESPONSE_FIELDS or payload.get("schema_version") != "mobile-guidance-suggestion-v1":
                raise MobileRuntimeError("导航建议字段不符合限定协议")
            if any(payload.get(key) != request.get(key) for key in ("request_id", "screen_hash", "catalog_id")) or type(payload.get("catalog_id")) is not int:
                raise MobileRuntimeError("导航建议未绑定当前目标和屏幕指纹")
            if self._ids(payload.get("task_ids")) != self._ids(request["task_ids"]):
                raise MobileRuntimeError("导航建议的监控任务集合不匹配")
            created = self._time(payload.get("created_at"))
            if not self._time(request["created_at"]) <= created <= self.now() or created > self._time(request["expires_at"]):
                raise MobileRuntimeError("导航建议创建时刻无效")
            actions = payload.get("actions")
            if not isinstance(actions, list) or not 1 <= len(actions) <= 3:
                raise MobileRuntimeError("每轮导航建议只接受1至3步")
            lines = request.get("ocr", [])
            if not isinstance(lines, list) or any(not isinstance(line, dict) for line in lines):
                raise MobileRuntimeError("导航请求缺少有效OCR文字证据")
            for action in actions:
                if not isinstance(action, dict):
                    raise MobileRuntimeError("导航动作格式无效")
                if action.get("kind") == "tap_text" and set(action) == {"kind", "text"} and isinstance(action.get("text"), str) and action["text"] in self.TEXTS:
                    if sum(_identity(line.get("text")) == _identity(action["text"]) for line in lines) != 1:
                        raise MobileRuntimeError("建议文字在原截图中没有唯一精确OCR证据")
                    continue
                if action == {"kind": "type_target_query"}:
                    if request.get("screen_kind") != "searchEdit":
                        raise MobileRuntimeError("原请求不是已核对的搜索编辑页，不接受输入建议")
                    continue
                if action.get("kind") == "tap_home_search" and set(action) == {"kind", "text"} and isinstance(action.get("text"), str):
                    text = action["text"]
                    # Count matching text in the whole screenshot before the
                    # region test: a duplicate feed caption is not an anchor.
                    anchors = [line for line in lines if _identity(line.get("text")) == _identity(text)]
                    chrome = [line for line in lines if _coordinate(line, "y") < .3 or _coordinate(line, "y") > .91]
                    sensitive = self.SENSITIVE.search("\n".join(str(line.get("text", "")) for line in chrome))
                    if (request.get("screen_kind") == "XianyuHome" and trusted_xianyu_home(lines)
                            and len(anchors) == 1 and text.strip() and len(text) <= 120 and not sensitive
                            and .03 <= _coordinate(anchors[0], "x") <= .8 and .14 <= _coordinate(anchors[0], "y") <= .25):
                        continue
                raise MobileRuntimeError("只接受限定导航文字或输入当前任务查询，不接受坐标、任意文本或业务操作")
            self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
            # O_EXCL prevents replacing or replaying an outstanding suggestion.
            if self.response_file.is_symlink() or self.response_file.exists():
                raise MobileRuntimeError("已有导航建议等待消费，不允许覆盖")
            current = self._read()
            if current != envelope:
                raise MobileRuntimeError("导航请求已更新，请重新查看截图")
            self._validate(current)
            temp = self.response_file.with_suffix(f".{os.getpid()}.tmp")
            try:
                with temp.open("x", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                temp.chmod(0o600)
                os.link(temp, self.response_file)
            except FileExistsError:
                raise MobileRuntimeError("已有导航建议等待消费，不允许覆盖") from None
            finally:
                temp.unlink(missing_ok=True)
            return dict(ok=True, status="suggestion_queued", request_id=request["request_id"], actions=len(actions), message="导航建议已入队；执行前仍须重新识别页面及核对监控任务")
