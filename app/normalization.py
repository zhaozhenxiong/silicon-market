"""Normalize scraped/imported observations without inventing sale information."""
from __future__ import annotations

import math
import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

CATEGORIES = {"gpu", "ram", "cpu", "motherboard", "storage", "computer", "component", "phone"}
STATUSES = {"on_sale", "sold", "delisted", "unknown"}
SHANGHAI = ZoneInfo("Asia/Shanghai")


class ValidationError(ValueError):
    pass


def utcnow():
    return datetime.now(timezone.utc)


def iso(value=None):
    if value is None or value == "":
        return utcnow().isoformat(timespec="microseconds")
    if isinstance(value, bool):
        raise ValidationError("时间不能是布尔值")
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise ValidationError("时间戳必须是有限数值")
        value = datetime.fromtimestamp(value / 1000 if value > 1e11 else value, timezone.utc)
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError("时间须为 ISO 8601 格式，例如 2026-10-30T12:00:00+08:00") from exc
    if not isinstance(value, datetime):
        raise ValidationError("时间格式不支持")
    if value.tzinfo is None:
        value = value.replace(tzinfo=SHANGHAI)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def number(value, field="price", nullable=False):
    if value is None or value == "":
        if nullable:
            return None
        raise ValidationError(f"缺少 {field}")
    if isinstance(value, bool):
        raise ValidationError(f"{field} 必须是有效价格")
    if isinstance(value, list):
        value = "".join(str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in value)
    if isinstance(value, str):
        value = value.strip().replace(",", "").replace("￥", "").replace("¥", "").replace("元", "")
        if re.fullmatch(r"\d+(?:\.\d+)?万", value):
            value = float(value[:-1]) * 10000
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} 必须是数字，不能是价格区间或面议") from exc
    if not math.isfinite(result) or result < 0 or result > 100_000_000:
        raise ValidationError(f"{field} 超出可记录范围")
    return round(result, 2)


def infer_phone(title):
    """Recognize a handset family/variant and an explicit storage capacity."""
    t = unicodedata.normalize("NFKC", title).upper()
    capacity_end = r"(?=\s*(?:(?:64|128|256|512|1024)\s*G|[12]\s*T)|[^A-Z0-9]|$)"
    patterns = [
        ("iPhone", r"(?<![A-Z0-9])(?:IPHONE|苹果)\s*(SE\s*(?:20(?:20|22)|[123])?|\d{1,2})\s*(PRO\s*MAX|PRO|PLUS|MINI|AIR)?" + capacity_end),
        ("Huawei", r"(?<![A-Z0-9])(?:华为|HUAWEI)\s*(MATE\s*\d{1,3}|PURA\s*\d{1,3}|P\s*\d{1,3}|NOVA\s*\d{1,3})\s*(PRO\s*\+|PRO|ULTRA|RS|SE)?" + capacity_end),
        ("Redmi", r"(?<![A-Z0-9])(?:红米|REDMI)\s*(K\s*\d{1,3}|NOTE\s*\d{1,3}|TURBO\s*\d{1,2}|\d{1,2}[AC]?)\s*(PRO\s*\+|PRO|ULTRA|至尊版)?" + capacity_end),
        ("Xiaomi", r"(?<![A-Z0-9])(?:小米|XIAOMI)\s*(MIX\s*(?:FOLD\s*)?\d{1,2}|\d{1,2})\s*(S\s*PRO|PRO|ULTRA|S)?" + capacity_end),
        ("Samsung", r"(?<![A-Z0-9])(?:三星|SAMSUNG)\s*(?:GALAXY\s*)?(S\s*\d{1,2}|NOTE\s*\d{1,2}|Z\s*(?:FOLD|FLIP)\s*\d{1,2}|A\s*\d{1,2})\s*(ULTRA|PLUS|\+|FE)?" + capacity_end),
        ("Vivo", r"(?<![A-Z0-9])VIVO\s*(X\s*\d{1,3}|S\s*\d{1,2}|Y\s*\d{1,3})\s*(PRO\s*\+|PRO|ULTRA|S)?" + capacity_end),
        ("iQOO", r"(?<![A-Z0-9])IQOO\s*(NEO\s*\d{1,2}|Z\s*\d{1,2}|\d{1,2})\s*(PRO|S)?" + capacity_end),
        ("Oppo", r"(?<![A-Z0-9])OPPO\s*(FIND\s*X\s*\d{1,2}|RENO\s*\d{1,2}|A\s*\d{1,3})\s*(PRO\s*\+|PRO|ULTRA)?" + capacity_end),
        ("OnePlus", r"(?<![A-Z0-9])(?:一加|ONEPLUS)\s*(ACE\s*\d{1,2}|\d{1,2})\s*(PRO|T|R)?" + capacity_end),
        ("Honor", r"(?<![A-Z0-9])(?:荣耀|HONOR)\s*(MAGIC\s*\d{1,2}|\d{2,3}|X\s*\d{1,2})\s*(PRO|至臻版|GT|LITE)?" + capacity_end),
    ]
    detected = None
    for brand, pattern in patterns:
        match = re.search(pattern, t)
        if match:
            family = re.sub(r"\s+", "", match[1])
            suffix = re.sub(r"\s+", " ", match[2] or "").strip()
            if suffix == "PROMAX":
                suffix = "PRO MAX"
            detected = f"{brand} {family}" + (" " + suffix if suffix else "")
            break
    if detected is None:
        return None
    pair = re.search(r"(?<![A-Z0-9])\d{1,2}\s*G?(?:B)?\s*\+\s*(64|128|256|512|1024)\s*(?:GB?|(?![A-Z0-9]))", t)
    single = re.search(r"(?<![A-Z0-9])(64|128|256|512|1024)\s*GB?(?![A-Z0-9])", t)
    tb = re.search(r"(?<![A-Z0-9])([12])\s*TB?(?![A-Z0-9])", t)
    # Storage may be attached directly to a model: iPhone15256G or S24Ultra256G.
    tail = t[match.end():]
    attached = re.match(r"\s*(64|128|256|512|1024)\s*GB?(?![A-Z0-9])", tail)
    attached_tb = re.match(r"\s*([12])\s*TB?(?![A-Z0-9])", tail)
    if pair or single or attached:
        capacity = (pair or single or attached)[1] + "GB"
    elif tb or attached_tb:
        capacity = (tb or attached_tb)[1] + "TB"
    else:
        capacity = "（容量待确认）"
    return detected + " " + capacity


def infer_hardware(title, category=None, model=None):
    t = unicodedata.normalize("NFKC", title).upper()
    compact = re.sub(r"\s+", " ", t)
    guess, detected = "component", "未识别元件"
    gpu = re.search(r"(?<![A-Z0-9])(RTX|GTX)\s*[- ]?(\d{3,4})\s*(TI\s*SUPER|SUPER|TI)?(?=\d{1,2}G(?:B)?(?![A-Z0-9])|[^A-Z0-9]|$)", compact)
    amd = re.search(r"(?<![A-Z0-9])RX\s*[- ]?(\d{3,4})\s*(XTX|XT|GRE)?(?=\d{1,2}G(?:B)?(?![A-Z0-9])|[^A-Z0-9]|$)", compact)
    arc = re.search(r"(?<![A-Z0-9])(?:ARC\s*)?([AB][578]\d0)(?=\d{1,2}G(?:B)?(?![A-Z0-9])|[^A-Z0-9]|$)", compact)
    intel = re.search(r"(?<![A-Z0-9])(I[3579])\s*[- ]?(\d{4,5}[A-Z]{0,3})(?![A-Z0-9])", compact)
    ryzen = re.search(r"(?<![A-Z0-9])(?:RYZEN\s*[3579]?\s*|R[3579]\s*[- ]?)([3579]\d{3}(?:X3D|XT|X|G|F)?)(?![A-Z0-9])", compact)
    component = re.search(r"(?<![A-Z0-9])(STM32[A-Z0-9]+|ESP32(?:-[A-Z0-9]+)?|ATMEGA\d+[A-Z]*|RP2040|CH340[A-Z]?|XC[0-9A-Z]+)(?![A-Z0-9])", compact)
    if gpu:
        guess, detected = "gpu", f"{gpu[1]} {gpu[2]}" + (f" {re.sub(r'TI\s*SUPER', 'TI SUPER', gpu[3])}" if gpu[3] else "")
    elif amd:
        guess, detected = "gpu", f"RX {amd[1]}" + (f" {amd[2]}" if amd[2] else "")
    elif arc:
        guess, detected = "gpu", f"Arc {arc[1]}"
    elif intel:
        guess, detected = "cpu", f"{intel[1].lower()}-{intel[2]}"
    elif ryzen:
        guess, detected = "cpu", f"Ryzen {ryzen[1]}"
    elif re.search(r"(?<![A-Z0-9])DDR\s*[345](?=\s*\d{1,3}G|[^A-Z0-9]|$)|内存", compact):
        guess, detected = "ram", "内存（规格待确认）"
    elif re.search(r"(?<![A-Z0-9])[ABHXZ][345678]\d{2}[ME]?(?![A-Z0-9])|主板", compact):
        guess, detected = "motherboard", "主板（型号待确认）"
    elif re.search(r"SSD|NVME|固态|硬盘", compact):
        guess, detected = "storage", "存储（规格待确认）"
    elif component:
        guess, detected = "component", component[1]
    if re.search(r"整机|台式电脑|电脑主机|笔记本|MACBOOK|MINI\s*PC", compact):
        guess, detected = "computer", "整机（配置待确认）"
        config = []
        if intel:
            config.append(f"{intel[1].lower()}-{intel[2]}")
        elif ryzen:
            config.append(f"Ryzen {ryzen[1]}")
        if gpu:
            config.append(f"{gpu[1]} {gpu[2]}" + (f" {gpu[3]}" if gpu[3] else ""))
        elif amd:
            config.append(f"RX {amd[1]}" + (f" {amd[2]}" if amd[2] else ""))
        if config:
            detected = "整机 " + " / ".join(config)
    phone = infer_phone(compact)
    if phone:
        guess, detected = "phone", phone
    elif re.search(r"手机|智能机", compact) and guess == "component":
        guess, detected = "phone", "手机（型号/容量待确认）"
    chosen = category if category and category != "all" else guess
    if not isinstance(chosen, str) or chosen not in CATEGORIES:
        raise ValidationError("不支持的硬件分类")
    if chosen == "ram":
        ddr = re.search(r"DDR\s*([345])", compact)
        # Separate a generation prefix before applying capacity boundaries:
        # DDR532G is 32GB, whereas C3616GX2 cannot establish a 16GB kit.
        memory_text = re.sub(r"(?<![A-Z0-9])DDR\s*[345]", " ", compact)
        pair = re.search(r"(?<![A-Z0-9])(\d{1,3})\s*G(?:B)?\s*[*X×]\s*([1248])(?![A-Z0-9])", memory_text)
        capacity = re.search(r"(?<![A-Z0-9])(\d{1,3})\s*G(?:B)?(?![A-Z0-9])", memory_text)
        speed = re.search(r"(?<![A-Z0-9])([23456789]\d{3})(?:\s*MHZ|(?![A-Z0-9]))", compact)
        parts = [f"DDR{ddr[1]}" if ddr else "内存"]
        if pair:
            parts.append(f"{int(pair[1]) * int(pair[2])}GB ({pair[1]}GB×{pair[2]})")
        elif capacity:
            parts.append(f"{capacity[1]}GB")
        else:
            parts.append("（容量待确认）")
        if speed:
            parts.append(speed[1])
        detected = " ".join(parts) if ddr or capacity else "内存（规格待确认）"
    elif chosen == "motherboard":
        chipset = re.search(r"(?<![A-Z0-9])([ABHXZ][345678]\d{2}[ME]?)(?![A-Z0-9])", compact)
        detected = chipset[1] if chipset else "主板（型号待确认）"
    elif chosen == "storage":
        capacity = re.search(r"(\d+(?:\.\d+)?)\s*(TB|GB|T|G)(?![A-Z0-9])", compact)
        detected = ("SSD " if re.search(r"SSD|固态|NVME", compact) else "存储 ") + (f"{capacity[1]}{'TB' if capacity[2] in ('T','TB') else 'GB'}" if capacity else "规格待确认")
    elif chosen != guess:
        detected = {"gpu":"显卡（型号待确认）", "cpu":"CPU（型号待确认）", "computer":"整机（配置待确认）", "component":"元件（规格待确认）", "phone":"手机（型号/容量待确认）"}.get(chosen, detected)
    # VRAM variants must not be combined (e.g. RTX 4060 Ti 8GB vs 16GB).
    if chosen == "gpu":
        vram_text = compact[(gpu or amd or arc).end():] if (gpu or amd or arc) else compact
        split_vram = re.search(r"(?<![A-Z0-9])\d(?:\s+\d)+\s*G(?:B)?(?![A-Z0-9])", vram_text)
        vram = re.search(r"(?<![A-Z0-9])(\d{1,2})\s*G(?:B)?(?![A-Z0-9])", vram_text)
        if split_vram:
            # '1 2G' may mean 12GB, but the isolated trailing '2G' is not a
            # reliable capacity. Preserve uncertainty instead of guessing.
            detected += " （显存规格待确认）"
        elif vram:
            detected += f" {vram[1]}GB"
    if model and str(model).strip().lower() not in {"auto", "all", "自动识别"}:
        detected = str(model).strip()[:160]
    return chosen, detected


def quality_flags(title, price, category):
    t = unicodedata.normalize("NFKC", title).lower()
    flags = []
    if re.search(r"求购|收购|回收|大量收|高价收|蹲一[张条个]|诚收", t):
        flags.append("求购/回收，非在售供给")
    if re.search(r"故障|坏卡|花屏|不亮|点不亮|维修件|尸体|料板|拆件|报废|仅亮机|不包好|时好时坏", t) and not re.search(r"无故障|没有故障|非故障", t):
        flags.append("故障/维修件")
    if re.search(r"空盒|包装盒|仅盒|盒子|支架|水冷头|散热器|转接线|延长线|挡板|背板|螺丝|导热垫", t) and category in {"gpu", "cpu", "ram", "motherboard", "storage"}:
        flags.append("配件/包装，非目标硬件")
    if category != "computer" and re.search(r"整机|电脑主机|全套电脑|搭配主板|板u套装|板u套餐", t):
        flags.append("整机/组合商品")
    if re.search(r"定金|订金|租赁|出租|分期|拍前咨询|价格私聊|标价非实价|价格非实价", t):
        flags.append("非完整售价/占位标价")
    if category == "phone":
        accessory_text = re.sub(r"(?:赠送|附赠|送|带|含)(?:一个|一套|原装)?(?:手机壳|保护壳|保护套|贴膜|钢化膜|充电器|充电线)", "", t)
        if re.search(r"手机壳|保护壳|保护套|贴膜|钢化膜|屏幕总成|屏幕组件|后盖|中框|手机支架|充电线|充电器|模型机|展示模型|扩容服务|解锁服务|维修服务", accessory_text):
            flags.append("手机配件/服务，非完整手机")
        problem_text = re.sub(r"无拆修|未拆修|无换屏|未换屏|没有换屏|无进水|没有进水|无烧屏|不烧屏|无漏液|无碎屏|无维修|未维修|无锁", "", t)
        if re.search(r"(?<!无)(?<!没有)(?<!非)有锁|官解|卡贴机|卡贴版|id锁|账户锁|账号锁|监管机|监管锁|黑解|激活锁|扩容机|改码机|组装机|妖机|进水|烧屏|漏液|碎屏|换屏|换主板|拆修", problem_text):
            flags.append("手机锁定/改装/维修或故障状态")
        if re.search(r"(?:64|128|256|512|1024)\s*(?:gb?)?\s*[/、或]\s*(?:64|128|256|512|1024)\s*gb?", t):
            flags.append("多容量报价，具体版本待确认")
    if category == "component" and price == 0:
        flags.append("零元占位/异常价格")
    elif category != "component" and price <= 10:
        flags.append("低价占位/异常价格（≤10元）")
    return flags


def unknown_model(model):
    return "待确认" in model or "未识别" in model


def explicit_model_in_title(target, title):
    """Match a custom ASCII model as a complete token, ignoring case/spacing.

    Keep boundaries in the original title: deleting every title space would
    incorrectly glue neighbouring brand names to the target model.
    """
    target = re.sub(r"\s+", "", unicodedata.normalize("NFKC", target).upper())
    if not target or not re.fullmatch(r"[A-Z0-9][A-Z0-9._/+()\-]*", target):
        return False
    pattern = r"(?<![A-Z0-9])" + r"\s*".join(re.escape(char) for char in target) + r"(?![A-Z0-9])"
    return bool(re.search(pattern, unicodedata.normalize("NFKC", title).upper()))


def normalize_record(record, task=None):
    if not isinstance(record, dict):
        raise ValidationError("每条记录须为 JSON 对象")
    task = task or {}
    info = record.get("商品信息") or record.get("product") or record
    if not isinstance(info, dict):
        raise ValidationError("商品信息须为 JSON 对象")
    # Older upstream JSONL and the public search exContent shape.
    data = info.get("data")
    item = data.get("item") if isinstance(data, dict) else None
    main = item.get("main") if isinstance(item, dict) else None
    if main and isinstance(main, dict):
        content = main.get("exContent")
        if isinstance(content, dict):
            info = {**content, **info}
    def pick(*keys, default=None):
        for key in keys:
            if info.get(key) is not None:
                return info[key]
            if record.get(key) is not None:
                return record[key]
        return default
    title = str(pick("title", "商品标题", "商品名称", "标题", default="")).strip()
    if not title or len(title) > 2000:
        raise ValidationError("缺少商品标题，或标题超过2000字")
    raw_price = pick("price", "当前售价", "商品价格", "售价", "价格")
    if isinstance(raw_price, dict):
        raw_price = raw_price.get("value") or raw_price.get("price")
    price = number(raw_price)
    url = str(pick("url", "商品链接", "链接", "targetUrl", default="")).strip()
    if url.startswith("//"):
        url = "https:" + url
    if url and not url.startswith(("https://", "http://")):
        url = ""  # Do not persist javascript:/app schemes as clickable links.
    item_id = pick("item_id", "商品ID", "商品id", "itemId", "id")
    if not item_id and url:
        item_id = parse_qs(urlparse(url).query).get("id", [None])[0]
    if item_id is None or not re.fullmatch(r"[A-Za-z0-9_:-]{1,160}", str(item_id)):
        raise ValidationError("缺少有效 item_id（或带 id 参数的商品链接）")
    item_id = str(item_id)
    category, model = infer_hardware(title, pick("category", default=task.get("category")), pick("model"))
    raw_status = str(pick("status", "状态", "商品状态", default="on_sale")).lower().strip()
    status = {"在售":"on_sale", "已售":"sold", "已售出":"sold", "卖掉了":"sold", "已下架":"delisted", "下架":"delisted", "未知":"unknown"}.get(raw_status, raw_status)
    if status not in STATUSES:
        raise ValidationError(f"未知商品状态: {raw_status}")
    evidence = str(pick("evidence", "状态证据", default="")).strip()[:4000]
    if status in {"sold", "delisted"} and not evidence:
        raise ValidationError("已售/下架记录必须提供 evidence（页面明示或人工核实），搜索缺失不算证据")
    sold_price = number(pick("sold_price", "成交价"), "sold_price", nullable=True)
    if sold_price is not None and (status != "sold" or not evidence):
        raise ValidationError("成交价只能随有明确证据的已售状态记录，不能由挂牌价推断")
    observed_at = iso(pick("observed_at", "爬取时间", "采集时间", "crawl_time"))
    if datetime.fromisoformat(observed_at).timestamp() > utcnow().timestamp() + 300:
        raise ValidationError("观测时间不能晚于当前时间5分钟以上")
    published = pick("published_at", "发布时间")
    # Relative phrases cannot establish a publication timestamp.
    if isinstance(published, str) and not re.match(r"^\d{4}-\d{2}-\d{2}", published):
        published = None
    published_at = iso(published) if published else None
    if published_at and published_at > observed_at:
        raise ValidationError("发布时间不能晚于观测时间")
    target = str(task.get("model") or "auto").strip()
    has_target = target.lower() not in {"auto", "all", "自动识别"}
    custom_target_matched = has_target and unknown_model(model) and explicit_model_in_title(target, title)
    if category == "phone" and custom_target_matched:
        # An explicit family name alone must not bypass missing storage data.
        custom_target_matched = bool(re.search(r"(?:64|128|256|512|1024)\s*GB?|[12]\s*TB?", target.upper()))
    if custom_target_matched:
        # Only resolve an otherwise unrecognized model, never relabel a known
        # but different GPU/CPU or discard its capacity/variant information.
        model = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", target)).strip()
    flags = quality_flags(title, price, category)
    if unknown_model(model):
        flags.append("型号/规格待确认，暂不合并行情")
    exclusions = task.get("exclude_keywords") or []
    if any(str(word).strip() and str(word).lower() in title.lower() for word in exclusions):
        flags.append("命中任务排除关键词")
    if task.get("min_price") is not None and price < float(task["min_price"]):
        flags.append("低于任务价格下限")
    if task.get("max_price") is not None and price > float(task["max_price"]):
        flags.append("高于任务价格上限")
    # A target model is a filter, never a licence to rename unrelated search results.
    if has_target:
        _, target_model = infer_hardware(target, category)
        if custom_target_matched:
            target_model = model
        def core(value):
            return re.sub(r"\s+", " ", re.sub(r"\s+\d+GB.*$", "", value.upper())).strip()
        if category == "gpu":
            mismatch = core(target_model) != core(model)
            target_vram = re.search(r"\s(\d+)GB", target_model)
            mismatch |= bool(target_vram and target_vram.group(0) not in model)
        else:
            # Match the explicit pieces specified by the target; extra speed/kit detail is allowed.
            target_tokens = target_model.upper().split()
            mismatch = any(token not in model.upper() for token in target_tokens)
        if mismatch:
            flags.append("与任务目标型号不符或规格不足")
    return dict(item_id=item_id, title=title, url=url, category=category, model=model,
                price=price, status=status, evidence=evidence, sold_price=sold_price,
                observed_at=observed_at, published_at=published_at, quality_flags=flags,
                excluded=bool(flags))
