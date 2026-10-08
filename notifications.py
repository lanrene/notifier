import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Dict, Optional

import requests


PUSHPLUS_URL = "https://www.pushplus.plus/send"


def _notification_key(title: str, content: str) -> str:
    payload = json.dumps(
        [title, content],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_notification_state(state_path: Path) -> Dict[str, int]:
    try:
        with state_path.open("r", encoding="utf-8") as state_file:
            state = json.load(state_file)
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError(f"读取通知状态文件失败：{e}") from e

    if (
        not isinstance(state, dict)
        or state.get("version") != 1
        or not isinstance(state.get("notifications"), dict)
    ):
        raise RuntimeError("通知状态文件格式无效")

    notifications = state["notifications"]
    if any(
        not isinstance(key, str)
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count < 0
        for key, count in notifications.items()
    ):
        raise RuntimeError("通知状态文件包含无效的通知记录")
    return notifications


def _save_notification_state(state_path: Path, notifications: Dict[str, int]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=state_path.parent,
            prefix=f".{state_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as state_file:
            temp_path = Path(state_file.name)
            json.dump(
                {"version": 1, "notifications": notifications},
                state_file,
                ensure_ascii=False,
                indent=2,
            )
            state_file.flush()
            os.fsync(state_file.fileno())
        os.replace(temp_path, state_path)
    except OSError as e:
        raise RuntimeError(f"保存通知状态文件失败：{e}") from e
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def send_notification(
    title: str,
    content: str,
    token: str,
    state_path: Path,
) -> Optional[str]:
    """Submit identical notifications via wechat, then cmcc, then suppress them."""
    if not token:
        raise RuntimeError("缺少 PUSHPLUS_TOKEN 环境变量，无法发送通知")

    notifications = _load_notification_state(state_path)
    notification_key = _notification_key(title, content)
    sent_count = notifications.get(notification_key, 0)
    if sent_count >= 2:
        return None

    channel = "wechat" if sent_count == 0 else "cmcc"
    try:
        response = requests.post(
            PUSHPLUS_URL,
            json={
                "token": token,
                "title": title,
                "content": content,
                "template": "markdown",
                "channel": channel,
            },
            timeout=20,
        )
        response.raise_for_status()
        result = response.json()
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"PushPlus 请求失败：{e}") from e
    except ValueError as e:
        raise RuntimeError(f"PushPlus 返回了无效 JSON：{e}") from e

    if not isinstance(result, dict) or result.get("code") != 200:
        if not isinstance(result, dict):
            raise RuntimeError("PushPlus 返回的数据格式无效")
        raise RuntimeError(f"PushPlus 发送失败：{result.get('msg', '未知错误')}")

    notifications[notification_key] = sent_count + 1
    _save_notification_state(state_path, notifications)
    return channel
