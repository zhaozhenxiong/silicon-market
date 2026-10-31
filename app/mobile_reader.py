"""Local OCR parser for actual mobile readouts, never coordinate-derived prices.

UI input/capture is performed only by the authorized CUA driver. This module
analyses saved images with Apple's on-device Vision framework and validates the
displayed date and currency text. No screen capture or OS input occurs here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OCR_BINARY = ROOT / "output/bin/mobile-ocr"
PARSER_VERSION = "vision-mobile-readout-v6"
CURRENCY_AMOUNT = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?"


class ReadoutError(ValueError):
    pass


def readout_from_lines(lines, start_date, end_date, price_lines=None, price_roi=None,
                       model_lines=None, query_lines=None):
    start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    if start > end or (end - start).days > 365:
        raise ReadoutError("日期窗口无效")
    clean = [line for line in lines if isinstance(line, dict) and isinstance(line.get("text"), str)]
    upper = "\n".join(line["text"] for line in clean if float(line.get("y", 0)) < .5)
    if re.search(r"镜像已锁定|使用触控\s*ID|输入.*登录信息", upper):
        return {"status": "locked", "message": "手机镜像等待用户本人解锁"}
    if re.search(r"连接已断开|无法连接.*iPhone|未连接", upper):
        return {"status": "disconnected", "message": "手机镜像连接不可用"}
    anchors = [line for line in clean if "近7日成交均价" in line["text"].replace(" ", "")]
    if len(anchors) != 1:
        raise ReadoutError("未识别唯一的近7日成交均价标签")
    anchor = anchors[0]
    label = re.sub(r"\s+", "", anchor["text"])
    mmdd = re.search(r"(?<!\d)(\d{1,2})[./月](\d{1,2})(?:日)?近7日成交均价", label)
    if not mmdd:
        raise ReadoutError("当前价格没有明确历史日期前缀，不能充当某日历史点")
    matches = []
    for year in range(start.year, end.year + 1):
        try:
            candidate = date(year, int(mmdd[1]), int(mmdd[2]))
        except ValueError:
            continue
        if start <= candidate <= end:
            matches.append(candidate.isoformat())
    if len(matches) != 1:
        raise ReadoutError("显示日期不属于目标窗口，或年份存在歧义")
    ay = float(anchor["y"])
    candidates = []
    # A separate Vision region excludes the weekly-change badge. If provided,
    # an unreadable crop must fail rather than falling back to merged text.
    price_source = clean if price_lines is None else price_lines
    for line in price_source:
        if not ay < float(line.get("y", 0)) < ay + .095 or float(line.get("x", 1)) > .55:
            continue
        text = re.sub(r"\s+", "", line["text"])
        # Vision may merge the large price with the adjacent weekly-change badge.
        # Only a known badge delimiter may be removed; arbitrary suffixes remain invalid.
        if price_lines is None:
            text = re.split(r"[较較]上[周週]", text, maxsplit=1)[0]
        price = re.fullmatch(r"[¥￥Y]\s*(" + CURRENCY_AMOUNT + r")", text)
        if price:
            candidates.append((line, float(price[1].replace(",", ""))))
    if len(candidates) != 1:
        raise ReadoutError("历史价格金额不唯一或未识别到明确货币金额")
    price_line, price = candidates[0]
    if not math.isfinite(price) or price < 0 or price > 100_000_000:
        raise ReadoutError("价格超出有效范围")
    full_price_text = price_line["text"]
    if price_lines is not None:
        full_prefixes = []
        for line in clean:
            if not ay < float(line.get("y", 0)) < ay + .095 or float(line.get("x", 1)) > .55:
                continue
            text = re.sub(r"\s+", "", line["text"])
            prefix = re.match(r"^[¥￥Y](" + CURRENCY_AMOUNT + r")(?![\d.,])", text)
            if prefix:
                full_prefixes.append((line, float(prefix[1].replace(",", ""))))
        # A prefix is corroboration only: an unknown merged suffix never becomes
        # a valid amount without the independent complete-currency ROI read.
        if len(full_prefixes) != 1 or full_prefixes[0][1] != price:
            raise ReadoutError("局部币值与整图主价格前缀不一致，可能被裁切或识别错误")
        full_price_text = full_prefixes[0][0]["text"]
        if price_roi is not None:
            try:
                edge = float(price_line["x"]) + float(price_line["width"])
                right = float(price_roi["right"])
                left = float(price_roi["left"])
                top = float(price_roi["top"])
                bottom = float(price_roi["bottom"])
                if (not 0 <= left < right <= 1 or not ay < top < bottom < ay + .12 or
                        edge >= right - .002 or float(price_line["x"]) <= left + .002):
                    raise ReadoutError("主价格靠近识别区域边界，无法排除币值截断")
            except (KeyError, TypeError, ValueError) as exc:
                if isinstance(exc, ReadoutError):
                    raise
                raise ReadoutError("主价格区域元数据无效") from exc
    # The search field is above category tabs; take only that relevant screen text.
    query_source = clean if query_lines is None else query_lines
    query_candidates = [l for l in query_source if isinstance(l, dict) and isinstance(l.get("text"), str) and .105 <= float(l.get("y", 0)) < .155 and .08 <= float(l.get("x", 0)) < .8]
    query_raw = " ".join(l["text"] for l in query_candidates)
    query_raw = re.sub(r"\s*[xX×]\s*$", "", query_raw).strip()
    title_source = clean if model_lines is None else model_lines
    title_lines = [l for l in title_source if isinstance(l, dict) and isinstance(l.get("text"), str) and .18 < float(l.get("y", 0)) < ay - .04 and re.search(r"[A-Za-z0-9]", l["text"]) and float(l.get("x", 0)) < .75]
    meaningful = [l["text"].strip() for l in title_lines if not re.search(r"人在搜|选择|CORE|搜索|回收|折|%", l["text"], re.I)]
    if model_lines is not None and len(meaningful) != 1:
        raise ReadoutError("独立型号区域未读取到唯一完整型号，不能回退误读标题")
    observed_model = meaningful[0] if meaningful else query_raw
    if not observed_model:
        raise ReadoutError("未读取到实际型号身份")
    observed_query = query_raw
    query_status = "read"
    if query_lines is not None:
        # The search box is small and may be unreadable even when the independent
        # large product title is exact. Do not claim or correct a conflicting
        # query. It is optional evidence; the backend still validates the actual
        # complete model and operator-verified economic scope.
        same = re.sub(r"\s+", "", query_raw).casefold() == re.sub(r"\s+", "", observed_model).casefold()
        if len(query_candidates) != 1 or not same:
            observed_query, query_status = None, "query_ocr_unreadable"
    return {"status": "read", "displayed_date": matches[0], "price": price,
            "metric": "rolling_7d_sold_mean", "observed_model": observed_model,
            "observed_query": observed_query, "query_ocr_raw": query_raw, "query_status": query_status,
            "ocr_text": f"Query OCR: {query_raw}\nModel ROI: {observed_model}\n{anchor['text']}\n{price_line['text']}\nFull OCR: {full_price_text}",
            "parser_version": PARSER_VERSION}


def readout_from_payload(payload, start_date, end_date):
    if payload.get("error"):
        raise ReadoutError("本机OCR无法读取图片")
    regions = payload.get("price_regions")
    if not regions:
        regions = [{"lines": payload.get("price_lines"), "roi": payload.get("price_roi")}]
    successes, failures = [], []
    for region in regions:
        try:
            result = readout_from_lines(payload["lines"], start_date, end_date, region.get("lines"), region.get("roi"), payload.get("model_lines"), payload.get("query_lines"))
            successes.append(result)
        except ReadoutError as exc:
            failures.append(str(exc))
    if not successes:
        raise ReadoutError("；".join(dict.fromkeys(failures)))
    if len({(item.get("status"), item.get("displayed_date"), item.get("price"), item.get("observed_model")) for item in successes}) != 1:
        raise ReadoutError("不同独立识别区域读数冲突，须复核原图")
    return successes[0]


def image_readout(path, start_date, end_date):
    path = Path(path).resolve(strict=True)
    if not OCR_BINARY.exists():
        raise ReadoutError("本机OCR尚未编译，请运行 automation/build_ocr.sh")
    run = subprocess.run([str(OCR_BINARY), str(path)], capture_output=True, text=True, timeout=20, check=True)
    payload = json.loads(run.stdout)
    result = readout_from_payload(payload, start_date, end_date)
    result.update(evidence_filename=path.name, evidence_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(image_readout(args.image, args.start, args.end), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"status": "needs_review", "message": str(exc)}, ensure_ascii=False))
        raise SystemExit(2)
