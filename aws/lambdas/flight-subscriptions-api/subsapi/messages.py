"""Traditional Chinese messages for each error code (same wording as the prototype)."""
from .months import label, selectable_months


def _fmt(n):
    return f"{n:,}"


def message(code, cfg, now, details=None):
    details = details or {}

    def name(c):
        p = cfg.place(c or "")
        return p["name_zh"] if p else (c or "")

    if code == "duplicate_subscription":
        o, _, d = (details.get("route") or "-").partition("-")
        month = details.get("departure_month")
        when = f" {label(month)}" if month else "（每月滾動）"
        return f"你已經訂閱過「{name(o)} → {name(d)}」{when}出發的航線，同一航線同一月份只能訂閱一次。"
    if code == "invalid_departure_month":
        months = selectable_months(now, cfg.first_offset_months, cfg.window_months)
        return f"請選擇 {label(months[0])} 到 {label(months[-1])} 之間的出發月份。"
    fixed = {
        "invalid_json": "請求格式不正確。",
        "origin_required": "請選擇出發地。",
        "destination_required": "請選擇目的地。",
        "unsupported_origin": "目前不支援這個出發地。",
        "unsupported_destination": "目前不支援這個目的地。",
        "same_origin_destination": "出發地與目的地不能相同。",
        "invalid_target_price": f"目標價請輸入 {_fmt(cfg.min_target)} 到 {_fmt(cfg.max_target)} 之間的整數（新台幣）。",
        "invalid_alerts_paused": "暫停狀態格式不正確。",
        "invalid_route": "訂閱代碼格式不正確。",
        "route_change_incomplete": "變更航線時，請同時提供出發地與目的地。",
        "empty_patch": "沒有要變更的內容。",
        "unknown_field": "包含不支援的欄位。",
        "invalid_plan": "不支援的方案。",
        "unauthorized": "請先登入。",
        "not_found": "找不到這筆訂閱，可能已被刪除。",
        "limit_reached": f"每位使用者最多 {cfg.max_subscriptions} 筆航線訂閱（已暫停與已過期的訂閱也算在內），請先刪除不需要的訂閱。",
        "conflict": "這筆訂閱剛剛被其他操作變更了，請重新整理後再試一次。",
        "not_found_route": "找不到這個 API 路徑。",
        "internal_error": "系統暫時發生錯誤，請稍後再試。",
    }
    return fixed.get(code, "發生錯誤。")
