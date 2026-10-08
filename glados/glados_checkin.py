import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from local_config import load_local_config
from notifications import send_notification


GLADOS_ORIGIN = "https://glados.rocks"
GLADOS_API = f"{GLADOS_ORIGIN}/api/user"
USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 16; Mobile) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Mobile Safari/537.36"
)


LOCAL_CONFIG = load_local_config({"GLADOS_COOKIE", "PUSHPLUS_TOKEN"})


def request_json(
    method: str,
    url: str,
    cookie: str,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    headers = {
        "cookie": cookie,
        "origin": GLADOS_ORIGIN,
        "user-agent": USER_AGENT,
    }
    try:
        response = requests.request(
            method,
            url,
            headers=headers,
            json=payload,
            timeout=20,
        )
        response.raise_for_status()
        result = response.json()
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"GLaDOS 请求失败：{e}") from e
    except ValueError as e:
        raise RuntimeError(f"GLaDOS 返回了无效 JSON：{e}") from e

    if not isinstance(result, dict):
        raise RuntimeError("GLaDOS 返回的数据格式无效")
    return result


def checkin(cookie: str, account_number: int) -> Tuple[str, bool, Optional[str]]:
    status = request_json("GET", f"{GLADOS_API}/status", cookie)
    if status.get("code") != 0:
        raise RuntimeError(status.get("message", "查询账号状态失败"))

    account = status.get("data")
    if not isinstance(account, dict):
        raise RuntimeError("账号状态数据格式无效")

    email = str(account.get("email", ""))
    nickname = email[:3] or f"账号 {account_number}"
    try:
        left_days = int(float(account.get("leftDays", "0")))
    except (TypeError, ValueError) as e:
        raise RuntimeError("账号剩余天数格式无效") from e

    checkin_result = request_json(
        "POST",
        f"{GLADOS_API}/checkin",
        cookie,
        {"token": "glados.rocks"},
    )
    if "code" in checkin_result and checkin_result["code"] != 0:
        raise RuntimeError(checkin_result.get("message", "签到失败"))
    message = checkin_result.get("message")
    if not isinstance(message, str):
        raise RuntimeError(f"签到失败：{checkin_result}")

    try:
        points_result = request_json("GET", f"{GLADOS_API}/points", cookie)
        points = int(float(points_result.get("points", "0")))
    except (RuntimeError, TypeError, ValueError) as e:
        return (
            f"{nickname}：{message}；签到已完成，积分查询失败",
            False,
            str(e) if isinstance(e, RuntimeError) else "账号积分格式无效",
        )

    exchange_message = ""
    exchanged = False
    if points >= 500:
        try:
            exchange_result = request_json(
                "POST",
                f"{GLADOS_API}/exchange",
                cookie,
                {"planType": "plan500"},
            )
            if exchange_result.get("code") != 0:
                raise RuntimeError(f"自动兑换失败：{exchange_result}")
        except RuntimeError as e:
            return (
                f"{nickname}：{message}；签到已完成，自动兑换失败；"
                f"积分 {points}；剩余 {left_days} 天",
                False,
                str(e),
            )
        points -= 500
        left_days += 100
        exchanged = True
        exchange_message = f"；自动兑换成功：{exchange_result.get('message', '')}"

    return (
        f"{nickname}：{message}{exchange_message}；积分 {points}；剩余 {left_days} 天",
        exchanged,
        None,
    )


def main() -> int:
    cookies = [
        cookie.strip()
        for cookie in os.environ.get(
            "GLADOS_COOKIE",
            LOCAL_CONFIG.get("GLADOS_COOKIE", ""),
        ).split("&")
        if cookie.strip()
    ]
    results = []
    failures = []
    follow_up_errors = []
    exchange_succeeded = False

    if not cookies:
        failures.append("未获取到 GLADOS_COOKIE")
    else:
        for account_number, cookie in enumerate(cookies, start=1):
            try:
                result, exchanged, follow_up_error = checkin(cookie, account_number)
                print(result)
                results.append(result)
                exchange_succeeded = exchange_succeeded or exchanged
                if follow_up_error:
                    follow_up_errors.append(
                        f"账号 {account_number}（签到已完成）：{follow_up_error}"
                    )
            except RuntimeError as e:
                message = f"账号 {account_number}：{e}"
                print(f"❌ {message}")
                results.append(message)
                failures.append(message)

    if failures:
        title = "GLaDOS 签到失败"
    elif follow_up_errors:
        title = "GLaDOS 签到完成但后续处理异常"
    else:
        title = "GLaDOS 签到成功"
    notification_channel = (
        "cmcc" if failures or follow_up_errors or exchange_succeeded else "wechat"
    )
    content = "\n".join(results)
    if failures or follow_up_errors:
        content = "\n".join(
            [*results, *(f"后续处理异常：{error}" for error in follow_up_errors)]
        )

    try:
        channel = send_notification(
            title,
            content,
            os.environ.get(
                "PUSHPLUS_TOKEN",
                LOCAL_CONFIG.get("PUSHPLUS_TOKEN", ""),
            ).strip(),
            channel=notification_channel,
            template="txt",
        )
        print(f"通知已通过 {channel} 渠道提交")
    except (RuntimeError, ValueError) as e:
        print(f"❌ 通知失败：{e}")
        failures.append(str(e))

    return 1 if failures or follow_up_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
