import requests
import json
import os
import time
import sys
import tempfile
from http.cookies import SimpleCookie
from datetime import datetime, timezone
from typing import List, Dict, Set, Optional, Tuple
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from local_config import load_local_config
from notifications import send_notification

# ====================== 用户配置区 ======================
LOCAL_CONFIG = load_local_config(
    {
        "EPIC_COOKIE",
        "PUSHPLUS_TOKEN",
        "EPIC_FORCE_ORDER_REFRESH",
    }
)
RAW_COOKIE = os.environ.get("EPIC_COOKIE", LOCAL_CONFIG.get("EPIC_COOKIE", "")).strip()
PUSHPLUS_TOKEN = os.environ.get(
    "PUSHPLUS_TOKEN",
    LOCAL_CONFIG.get("PUSHPLUS_TOKEN", ""),
).strip()
ORDER_JSON_PATH = SCRIPT_DIR / "epic_orders.json"
NOTIFICATION_STATE_PATH = SCRIPT_DIR / "epic_notification_state.json"
FORCE_ORDER_REFRESH = os.environ.get(
    "EPIC_FORCE_ORDER_REFRESH",
    LOCAL_CONFIG.get("EPIC_FORCE_ORDER_REFRESH", ""),
) == "1"
PAGE_DELAY = 1.6  # 全量分页请求延时(秒)
# ======================================================


class EpicRequestError(Exception):
    """Epic 请求失败。"""


def fetch_one_page(raw_cookie: str, next_page_token: Optional[str] = None) -> Dict:
    """拉取单页订单，count=10，和浏览器保持一致"""
    cookie_obj = SimpleCookie()
    cookie_obj.load(raw_cookie)
    xsrf_token = cookie_obj.get("XSRF-AM-TOKEN")
    if not xsrf_token:
        raise EpicRequestError("Cookie 缺少 XSRF-AM-TOKEN")

    base_url = "https://accounts.epicgames.com/account/v2/payment/ajaxGetOrderHistory"
    headers = {
        "Cookie": raw_cookie,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36 Edg/154.0.0.0",
        "x-xsrf-token": xsrf_token.value,
        "x-requested-with": "XMLHttpRequest",
        "accept": "application/json, text/plain, */*",
        "referer": "https://accounts.epicgames.com/",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6"
    }
    params = {
        "count": "10",
        "sortDir": "DESC",
        "sortBy": "DATE",
        "locale": "zh-Hans",
    }
    if next_page_token:
        params["nextPageToken"] = next_page_token

    try:
        resp = requests.get(base_url, params=params, headers=headers, timeout=20)
        if resp.status_code == 403:
            raise EpicRequestError("HTTP 403：Epic 拒绝请求，Cookie 或 cf_clearance 可能已失效")
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict) or not isinstance(data.get("orders"), list):
            raise EpicRequestError("订单接口返回的数据格式无效")
        return data
    except requests.exceptions.RequestException as e:
        raise EpicRequestError(f"订单请求网络异常：{e}") from e
    except ValueError as e:
        raise EpicRequestError(f"订单接口返回了无效 JSON：{e}") from e


def filter_useful_orders(raw_orders: List[Dict]) -> List[Dict]:
    """只保留订单必要字段"""
    out = []
    for o in raw_orders:
        items_slim = []
        for it in o.get("items", []):
            items_slim.append({
                "description": it.get("description"),
                "offerId": it.get("offerId"),
                "namespace": it.get("namespace")
            })
        out.append({
            "orderId": o.get("orderId"),
            "items": items_slim
        })
    return out


def fetch_full_all_orders(raw_cookie: str) -> List[Dict]:
    """全量拉取全部订单，每页延时"""
    print("🔍 需要全量拉取账号全部订单，请稍候...")
    all_raw = []
    next_token = None
    seen_tokens: Set[str] = set()
    page_idx = 0
    while True:
        page_idx += 1
        resp_data = fetch_one_page(raw_cookie, next_token)
        page_orders = resp_data.get("orders", [])
        if not page_orders:
            break
        all_raw.extend(page_orders)
        print(f"    第{page_idx}页 获取 {len(page_orders)} 条订单")
        next_token = resp_data.get("nextPageToken")
        if not next_token:
            break
        if next_token in seen_tokens:
            raise EpicRequestError("订单分页接口重复返回 nextPageToken，已停止避免无限循环")
        seen_tokens.add(next_token)
        time.sleep(PAGE_DELAY)
    return filter_useful_orders(all_raw)


def fetch_incremental_orders(raw_cookie: str, local_orders: List[Dict]) -> List[Dict]:
    """拉取最新订单，遇到本地已知订单时停止翻页。"""
    known_ids = {
        order.get("orderId")
        for order in local_orders
        if order.get("orderId")
    }
    new_orders = []
    next_token = None
    seen_tokens: Set[str] = set()
    page_idx = 0

    while True:
        page_idx += 1
        response = fetch_one_page(raw_cookie, next_token)
        page_orders = filter_useful_orders(response["orders"])
        found_known_order = False
        for order in page_orders:
            order_id = order.get("orderId")
            if order_id and order_id in known_ids:
                found_known_order = True
            elif order_id:
                new_orders.append(order)
                known_ids.add(order_id)

        print(f"    增量第{page_idx}页 获取 {len(page_orders)} 条订单")
        if found_known_order:
            break

        next_token = response.get("nextPageToken")
        if not next_token:
            break
        if next_token in seen_tokens:
            raise EpicRequestError("订单分页接口重复返回 nextPageToken，已停止避免无限循环")
        seen_tokens.add(next_token)
        time.sleep(PAGE_DELAY)

    return merge_orders(local_orders, new_orders)


def load_local_order_file() -> Optional[Dict]:
    """读取本地订单json"""
    if not ORDER_JSON_PATH.exists():
        return None
    try:
        with open(ORDER_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("orders"), list):
            raise ValueError("JSON 必须包含 orders 数组")
        return data
    except (OSError, json.JSONDecodeError, ValueError) as e:
        print(f"⚠️ 读取本地订单文件异常: {e}")
        return None


def save_order_json(data: Dict):
    """通过同目录临时文件原子替换订单 JSON。"""
    ORDER_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=ORDER_JSON_PATH.parent,
            prefix=f".{ORDER_JSON_PATH.name}.",
            suffix=".tmp",
            delete=False,
        ) as f:
            temp_path = Path(f.name)
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, ORDER_JSON_PATH)
    except Exception as save_error:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError as cleanup_error:
                raise RuntimeError(
                    f"保存订单文件失败：{save_error}；"
                    f"清理临时文件也失败：{cleanup_error}"
                ) from save_error
        raise


def merge_orders(local_orders: List[Dict], new_page_orders: List[Dict]) -> List[Dict]:
    """按orderId去重合并订单"""
    exist_ids: Set[str] = {
        order.get("orderId")
        for order in local_orders
        if order.get("orderId")
    }
    merged = local_orders.copy()
    for item in new_page_orders:
        oid = item.get("orderId")
        if oid and oid not in exist_ids:
            merged.append(item)
            exist_ids.add(oid)
    return merged


def build_owned_set(order_list: List[Dict]) -> Set[str]:
    """构造已拥有游戏集合 namespace:offerId"""
    owned = set()
    for order in order_list:
        for it in order.get("items", []):
            ns = it.get("namespace")
            oid = it.get("offerId")
            if ns and oid:
                owned.add(f"{ns}:{oid}")
    return owned


def get_epic_free_games() -> Tuple[Optional[List[Dict]], Optional[str]]:
    """成功时返回游戏列表（可为空）；失败时返回错误原因。"""
    url = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        return None, f"请求免费游戏接口失败：{e}"
    except ValueError as e:
        return None, f"免费游戏接口返回无效 JSON：{e}"

    try:
        elements = data["data"]["Catalog"]["searchStore"]["elements"]
        if not isinstance(elements, list):
            raise ValueError("elements 不是数组")
        game_list = []
        now_utc = datetime.now(timezone.utc)

        for game in elements:
            promotions = game.get("promotions")
            if not promotions:
                continue
            active_promos = promotions.get("promotionalOffers", [])
            if not active_promos or not active_promos[0].get("promotionalOffers"):
                continue
            promo_item = active_promos[0]["promotionalOffers"][0]
            start = datetime.fromisoformat(promo_item["startDate"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(promo_item["endDate"].replace("Z", "+00:00"))
            total_price = game["price"]["totalPrice"]["discountPrice"]
            if not (start <= now_utc <= end and total_price == 0):
                continue

            title = game["title"]
            namespace = game.get("namespace", "")
            offer_id = game.get("id", "")
            mappings = game.get("catalogNs", {}).get("mappings", [])
            page_slug = mappings[0].get("pageSlug") if mappings else None

            if page_slug:
                link = f"https://store.epicgames.com/p/{page_slug}"
            else:
                link = "https://store.epicgames.com/free-games"

            game_list.append({
                "name": title,
                "url": link,
                "key": f"{namespace}:{offer_id}"
            })
        return game_list, None
    except (AttributeError, KeyError, TypeError, IndexError, ValueError) as e:
        return None, f"免费游戏接口返回的数据格式无效：{e}"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main() -> int:
    free_games, free_games_error = get_epic_free_games()
    if free_games_error:
        print(f"❌ {free_games_error}")
        free_games = []
    else:
        print(f"✅ 获取到 {len(free_games)} 款本周可领取游戏")

    if free_games_error is None and not free_games and not FORCE_ORDER_REFRESH:
        print("ℹ️ 当前没有可领取的 Epic 免费游戏")
        return 0

    local_data = load_local_order_file()
    owned_set: Set[str] = set()
    if local_data is not None:
        owned_set = build_owned_set(local_data["orders"])

    need_query_order = (
        FORCE_ORDER_REFRESH
        or local_data is None
        or not local_data.get("is_full", False)
    )
    if not need_query_order and free_games:
        need_query_order = any(game["key"] not in owned_set for game in free_games)

    failures = []
    if free_games_error:
        failures.append(f"限免游戏获取失败：{free_games_error}")

    orders_available = local_data is not None
    if need_query_order:
        if not RAW_COOKIE:
            order_error = "缺少 EPIC_COOKIE 环境变量"
        else:
            try:
                if local_data is None or not local_data.get("is_full", False):
                    print("🔍 全量拉取账号订单")
                    updated_orders = fetch_full_all_orders(RAW_COOKIE)
                    is_full = True
                else:
                    print("🔍 分页拉取本地最新订单之后的新增订单")
                    updated_orders = fetch_incremental_orders(
                        RAW_COOKIE,
                        local_data["orders"],
                    )
                    is_full = local_data.get("is_full", False)

                updated_owned_set = build_owned_set(updated_orders)
                updated_data = {
                    "modify_date": local_data.get("modify_date") if local_data else None,
                    "is_full": is_full,
                    "total_games": len(updated_owned_set),
                    "orders": updated_orders,
                }
                changed = (
                    local_data is None
                    or updated_orders != local_data["orders"]
                    or updated_data["is_full"] != local_data.get("is_full")
                    or updated_data["total_games"] != local_data.get("total_games")
                )
                if changed:
                    updated_data["modify_date"] = utc_now_iso()
                    save_order_json(updated_data)
                    print(
                        f"✅ 订单数据已更新：{len(updated_orders)} 条订单，"
                        f"{len(updated_owned_set)} 款游戏"
                    )
                else:
                    print("✅ 订单数据无变化，跳过写入")
                owned_set = updated_owned_set
                orders_available = True
                order_error = None
            except EpicRequestError as e:
                order_error = str(e)
        if order_error:
            failures.append(f"订单请求失败：{order_error}")
            print(f"❌ {order_error}")
            orders_available = False
    elif local_data is not None and local_data.get("total_games") != len(owned_set):
        local_data["total_games"] = len(owned_set)
        save_order_json(local_data)
        print(f"✅ 已修正本地游戏总数：{len(owned_set)}款")

    claimable_games = (
        free_games
        if not orders_available
        else [game for game in free_games if game["key"] not in owned_set]
    )

    print("\n==== 本周可领取的Epic游戏 ====")
    for game in free_games:
        if not orders_available:
            status_text = "⚠️ 尚未核验（订单数据不可用）"
        else:
            status_text = "✅ 已在库中" if game["key"] in owned_set else "❌ 尚未拥有"
        print(f"游戏名称：{game['name']}")
        print(f"是否入库：{status_text}")
        print(f"领取链接：{game['url']}\n")

    should_notify = bool(failures or claimable_games)
    if should_notify:
        content_lines = []
        if failures:
            content_lines.extend(f"- {failure}" for failure in failures)
        if claimable_games:
            section_title = (
                "### 入库状态未核验的限免游戏"
                if not orders_available
                else "### 可领取游戏"
            )
            content_lines.append(section_title)
            for game in claimable_games:
                ownership_note = "" if orders_available else "（入库状态未核验）"
                content_lines.append(f"- [{game['name']}]({game['url']}){ownership_note}")
        if free_games_error:
            content_lines.append("限免游戏列表不可用，无法确认本次是否还有其他可领取游戏。")
        try:
            channel = send_notification(
                "Epic 限免游戏提醒" if claimable_games and not failures else "Epic 自动任务异常",
                "\n".join(content_lines),
                PUSHPLUS_TOKEN,
                channel="custom",
                state_path=NOTIFICATION_STATE_PATH,
                template="markdown",
            )
            if channel is None:
                print("ℹ️ 相同通知已达到发送次数上限，跳过发送")
            else:
                print(f"✅ PushPlus 通知已通过 {channel} 渠道提交")
        except RuntimeError as e:
            print(f"❌ {e}")
            failures.append(str(e))

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
