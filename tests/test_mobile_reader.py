import pytest
from app.mobile_reader import ReadoutError, readout_from_lines, readout_from_payload


def screen(label="07.31近7日成交均价", price="¥2399", query="14900kf"):
    return [dict(text=query,x=.13,y=.126,height=.02),dict(text="14900kf",x=.33,y=.216,height=.02),
            dict(text=label,x=.05,y=.35,height=.02),dict(text=price,x=.04,y=.385,height=.04)]


def test_real_displayed_date_and_large_price_not_chart_coordinate():
    result=readout_from_lines(screen(),"2026-05-03","2026-10-29")
    assert result["displayed_date"]=="2026-07-31"
    assert result["price"]==2399
    assert result["observed_model"]=="14900kf"


@pytest.mark.parametrize("text,value",[("¥2300較上周→¥49",2300),("¥2200較上周 →¥50",2200),("￥2,399",2399)])
def test_adjacent_weekly_badge_does_not_replace_price(text,value):
    assert readout_from_lines(screen(price=text),"2026-05-03","2026-10-29")["price"]==value


@pytest.mark.parametrize("label",["近7日成交均价","02.30近7日成交均价","11.01近7日成交均价"])
def test_undated_invalid_or_outside_window_points_are_not_history(label):
    with pytest.raises(ReadoutError):readout_from_lines(screen(label=label),"2026-05-03","2026-10-29")


def test_mixed_prices_and_unknown_suffix_are_rejected():
    lines=screen();lines.append(dict(text="¥100",x=.2,y=.39,height=.03))
    with pytest.raises(ReadoutError):readout_from_lines(lines,"2026-05-03","2026-10-29")
    with pytest.raises(ReadoutError):readout_from_lines(screen(price="¥2399起或面议"),"2026-05-03","2026-10-29")


def test_year_boundary_is_resolved_only_with_window():
    assert readout_from_lines(screen(label="12.31近7日成交均价"),"2025-11-01","2026-04-29")["displayed_date"]=="2025-12-31"


def test_locked_state_is_not_zero_price():
    assert readout_from_lines([dict(text="iPhone镜像已锁定",y=.2)],"2026-05-03","2026-10-29")["status"]=="locked"


def roi_line(text="¥2600", **changes):
    return dict(text=text, x=.04, y=.385, width=.27, height=.04, **changes)


def test_independent_strict_roi_recovers_unknown_merged_badge_only_with_matching_full_prefix():
    result = readout_from_lines(screen(label="06.09近7日成交均价", price="¥2600袋上周 ¥216"),
                                "2026-05-03", "2026-10-29", [roi_line()],
                                dict(left=.025, right=.335, top=.375, bottom=.435))
    assert result["displayed_date"] == "2026-06-09" and result["price"] == 2600
    assert "Full OCR: ¥2600袋上周 ¥216" in result["ocr_text"]


@pytest.mark.parametrize("full,crop", [("¥15000袋上周 ¥216", "¥1500"), ("¥2600袋上周 ¥216", "¥260"),
                                      ("¥2600袋上周 ¥216", "¥216"), ("金额待咨询", "¥2600")])
def test_truncated_or_different_roi_currency_is_rejected(full, crop):
    with pytest.raises(ReadoutError, match="不一致"):
        readout_from_lines(screen(price=full), "2026-05-03", "2026-10-29", [roi_line(crop)])


def test_roi_touching_crop_edge_is_rejected_even_when_prefix_appears_to_agree():
    line = roi_line()
    line["width"] = .295
    with pytest.raises(ReadoutError, match="截断"):
        readout_from_lines(screen(price="¥2600袋上周 ¥216"), "2026-05-03", "2026-10-29", [line],
                           dict(left=.025, right=.335, top=.375, bottom=.435))


@pytest.mark.parametrize("crop", [[], [roi_line("2600")], [roi_line("¥2600袋上周")], [roi_line(), roi_line("¥216")]])
def test_unreadable_or_ambiguous_roi_never_falls_back_to_global_price(crop):
    with pytest.raises(ReadoutError):
        readout_from_lines(screen(price="¥2600較上周 ¥216"), "2026-05-03", "2026-10-29", crop)


def test_full_five_digit_currency_is_accepted_when_complete_roi_preserves_width():
    line = roi_line("¥15000")
    line["width"] = .35
    result = readout_from_lines(screen(price="¥15000"), "2026-05-03", "2026-10-29", [line],
                                dict(left=.025, right=.42, top=.375, bottom=.435))
    assert result["price"] == 15000


@pytest.mark.parametrize("amount", ["¥26,00", "¥2,,600", "¥2600.000", "¥2600."])
def test_currency_grouping_or_incomplete_decimals_are_not_strict_amounts(amount):
    with pytest.raises(ReadoutError):
        readout_from_lines(screen(price=amount), "2026-05-03", "2026-10-29")


def test_independent_model_roi_recovers_actual_identity_without_creating_error_aliases():
    lines = screen(query="12900k「x")
    lines[1]["text"] = "14900ki"
    model = [dict(text="14900kf", x=.34, y=.217, width=.19, height=.016)]
    query = [dict(text="14900kT x", x=.14, y=.126, width=.15, height=.012)]
    result = readout_from_lines(lines, "2026-05-03", "2026-10-29", model_lines=model, query_lines=query)
    assert result["observed_model"] == "14900kf"
    assert result["observed_query"] is None
    assert result["query_status"] == "query_ocr_unreadable"
    assert result["query_ocr_raw"] == "14900kT"
    assert "Query OCR: 14900kT" in result["ocr_text"]
    assert "Model ROI: 14900kf" in result["ocr_text"]


def test_other_catalog_identity_is_not_corrected_or_coerced_to_previous_model():
    model = [dict(text="12900kf", x=.34, y=.217, width=.19, height=.016)]
    query = [dict(text="12900KF x", x=.14, y=.126, width=.15, height=.012)]
    result = readout_from_lines(screen(), "2026-05-03", "2026-10-29", model_lines=model, query_lines=query)
    assert result["observed_model"] == "12900kf"
    assert result["observed_query"] == "12900KF"
    assert result["query_status"] == "read"


@pytest.mark.parametrize("models", [[], [dict(text="14900kf",x=.34,y=.217), dict(text="12900kf",x=.34,y=.22)]])
def test_ambiguous_or_unreadable_independent_title_never_falls_back_to_full_frame(models):
    with pytest.raises(ReadoutError, match="独立型号"):
        readout_from_lines(screen(), "2026-05-03", "2026-10-29", model_lines=models)


def test_alternate_exact_region_recovers_edge_rejection_without_dropping_truncation_guard():
    original = roi_line("¥2410")
    original.update(x=.03933, width=.29512)
    wider = roi_line("¥2410")
    wider.update(x=.03933, width=.29512)
    payload = dict(lines=screen(label="09.08近7日成交均价", price="¥2410絞上同+¥189"), price_regions=[
        dict(lines=[original], roi=dict(left=.025, right=.335, top=.375, bottom=.435)),
        dict(lines=[wider], roi=dict(left=.025, right=.35, top=.375, bottom=.435)),
    ])
    assert readout_from_payload(payload, "2026-05-03", "2026-10-29")["price"] == 2410


def test_alternate_regions_cannot_accept_truncated_15000_as_1500():
    payload = dict(lines=screen(price="¥15000絞上同+¥189"), price_regions=[
        dict(lines=[roi_line("¥1500")], roi=dict(left=.025, right=.335, top=.375, bottom=.435)),
        dict(lines=[roi_line("¥1500")], roi=dict(left=.025, right=.35, top=.375, bottom=.435)),
    ])
    with pytest.raises(ReadoutError, match="不一致"):
        readout_from_payload(payload, "2026-05-03", "2026-10-29")


def test_alternate_region_unknown_suffix_never_becomes_a_valid_currency():
    payload = dict(lines=screen(price="¥2410絞上同+¥189"), price_regions=[
        dict(lines=[roi_line("¥2410絞")], roi=dict(left=.025, right=.35, top=.375, bottom=.435)),
    ])
    with pytest.raises(ReadoutError):
        readout_from_payload(payload, "2026-05-03", "2026-10-29")
