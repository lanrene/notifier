import base64
import binascii
import copy
import hashlib
import hmac
import json
import os
import random
import re
import secrets
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import requests
from Cryptodome.Cipher import AES, PKCS1_v1_5
from Cryptodome.PublicKey import RSA
from Cryptodome.Util.Padding import pad

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from local_config import load_local_config
from notifications import send_notification
from mcloud_device import (
    ANDROID_VERSION,
    APP_VERSION,
    APP_VERSION_HEADER,
    CHROME_VERSION,
    DEVICE_BRAND,
    DEVICE_MODEL,
    FILE_API_CLIENT_INFO,
    FILE_API_USER_AGENT,
    SCREEN_HEIGHT,
    SCREEN_WIDTH,
    USER_AGENT,
)


LOCAL_CONFIG = load_local_config(
    {
        "MCLOUD_COOKIES",
        "MCLOUD_AUTH_ENCRYPTION_KEY",
        "PUSHPLUS_TOKEN",
    }
)
DATA_DIR = Path(os.environ.get("MCLD_DATA_DIR", str(Path(__file__).resolve().parent)))
STATE_PATH = DATA_DIR / "mcloud_data.json"
AI_CAMERA_IMAGE_DIR = Path(__file__).resolve().parent / "pic"

MARKET_BASE_URL = "https://m.mcloud.139.com"
JWT_URL = "https://caiyun.feixin.10086.cn:7071/portal/auth/tyrzLogin.action"
SPEC_TOKEN_URL = "https://orches.yun.139.com/orchestration/auth-rebuild/token/v1.0/querySpecToken"
REFRESH_TOKEN_URL = "https://user-njs.yun.139.com/user/auth/refreshToken"
DEVICE_PROFILE_URL = "https://slw.h5cmpassport.com:9090/deviceprofile/v4"
MARKET_USER_AGENT = USER_AGENT
REFRESH_TOKEN_AES_KEY = "c7lXOigXahPnTViq"
DEVICE_PROFILE_PUBLIC_KEY = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQC8KHAcHbkCn5rxGgGJE+07tY+pt86D/"
    "oZ7sA51FaEBv2jgno2TI9zHJVYKJynmiKpixgwUcv93EfWIrU/p/UCs5Vu+odS3I4UBp3R7"
    "IZ1A0W01FkumAHYW2PQpMm8ueQKPLUq/idkpG/9b2JDv/qU+Ks36nbUPwlW4CjdfrV+V9Q"
    "IDAQAB"
)
DEVICE_PROFILE_ORGANIZATION = "FXlyfmWg2AzwbrxDKSv5"
FIVE_DAYS_MS = 5 * 24 * 60 * 60 * 1000
TOKEN_EXPIRE_SECONDS_FALLBACK = 30 * 24 * 60 * 60
CLOUD_TASK_GROUPS = (
    ("cloudEmail", "联动任务"),
    ("time", "新版热门任务"),
    ("day", "云盘每日任务"),
    ("month", "云盘每月任务"),
)
CLOUD_FILE_CONTENT = b"0"
CLOUD_FILE_HASH = hashlib.sha256(CLOUD_FILE_CONTENT).hexdigest()
CLIENT_VERSION = APP_VERSION
AUTH_ENCRYPTION_ALGORITHM = "AES-256-GCM"
AUTH_ENCRYPTION_AAD_PREFIX = b"mcloud-authorization:v1:"
ACCOUNT_INDEX_PREFIX = b"mcloud-account-index:v1:"
RequestConfirmation = Callable[
    [
        str,
        str,
        Optional[Dict[str, str]],
        Optional[Dict[str, str]],
        Optional[Dict[str, str]],
        Any,
    ],
    None,
]

def _now_ms() -> int:
    return int(time.time() * 1000)


def _normalize_authorization(value: str) -> str:
    value = value.strip()
    return value if not value or value.startswith("Basic ") else f"Basic {value}"


def _token_expiry(authorization: str) -> int:
    try:
        encoded = authorization.removeprefix("Basic ").strip()
        decoded = base64.b64decode(encoded).decode("utf-8")
        fields = decoded.split("|")
        return int(fields[3]) if len(fields) > 3 and fields[3].isdigit() else 0
    except (ValueError, UnicodeDecodeError):
        return 0


def _build_authorization(account: str, raw_token: str) -> str:
    token = base64.b64encode(f"mobile:{account}:{raw_token}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _authorization_encryption_key() -> bytes:
    encoded_key = os.environ.get(
        "MCLOUD_AUTH_ENCRYPTION_KEY",
        LOCAL_CONFIG.get("MCLOUD_AUTH_ENCRYPTION_KEY", ""),
    ).strip()
    try:
        key = bytes.fromhex(encoded_key)
    except ValueError as error:
        raise RuntimeError(
            "MCLOUD_AUTH_ENCRYPTION_KEY 必须是 64 位十六进制字符串（32 字节）"
        ) from error
    if len(key) != 32:
        raise RuntimeError(
            "MCLOUD_AUTH_ENCRYPTION_KEY 必须是 64 位十六进制字符串（32 字节）"
        )
    return key


def _account_state_key(phone: str) -> str:
    digest = hmac.new(
        _authorization_encryption_key(),
        ACCOUNT_INDEX_PREFIX + phone.encode("utf-8"),
        hashlib.sha256,
    )
    return digest.hexdigest()


def _encrypt_authorization(account_key: str, authorization: str) -> Dict[str, str]:
    key = _authorization_encryption_key()
    cipher = AES.new(key, AES.MODE_GCM, nonce=secrets.token_bytes(12))
    cipher.update(AUTH_ENCRYPTION_AAD_PREFIX + account_key.encode("ascii"))
    ciphertext, tag = cipher.encrypt_and_digest(authorization.encode("utf-8"))
    return {
        "algorithm": AUTH_ENCRYPTION_ALGORITHM,
        "nonce": base64.b64encode(cipher.nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        "tag": base64.b64encode(tag).decode("ascii"),
    }


def _decrypt_authorization(account_key: str, encrypted: Dict[str, Any]) -> str:
    if encrypted.get("algorithm") != AUTH_ENCRYPTION_ALGORITHM:
        raise RuntimeError("移动云盘状态文件中的 Authorization 加密格式不受支持")
    try:
        key = _authorization_encryption_key()
        nonce = base64.b64decode(encrypted["nonce"], validate=True)
        ciphertext = base64.b64decode(encrypted["ciphertext"], validate=True)
        tag = base64.b64decode(encrypted["tag"], validate=True)
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        cipher.update(AUTH_ENCRYPTION_AAD_PREFIX + account_key.encode("ascii"))
        return cipher.decrypt_and_verify(ciphertext, tag).decode("utf-8")
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, binascii.Error) as error:
        raise RuntimeError(
            "移动云盘状态文件中的 Authorization 解密失败；请检查密钥是否正确且未被更改"
        ) from error


def _load_state() -> Dict[str, Dict[str, Any]]:
    try:
        with STATE_PATH.open("r", encoding="utf-8") as state_file:
            state = json.load(state_file)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"读取移动云盘状态文件失败：{error}") from error
    if not isinstance(state, dict) or not isinstance(state.get("accounts", {}), dict):
        raise RuntimeError("移动云盘状态文件格式无效")
    version = state.get("version", 1)
    if version not in {1, 2}:
        raise RuntimeError(f"移动云盘状态文件版本不受支持：{version}")
    stored_accounts = state.get("accounts", {})
    if any(not isinstance(key, str) or not isinstance(account, dict) for key, account in stored_accounts.items()):
        raise RuntimeError("移动云盘账号缓存格式无效")
    accounts: Dict[str, Dict[str, Any]] = {}
    for stored_key, stored_account in stored_accounts.items():
        if version == 1:
            phone = stored_key
            account_key = _account_state_key(phone)
        else:
            if not re.fullmatch(r"[0-9a-f]{64}", stored_key):
                raise RuntimeError("移动云盘状态文件中的账号索引格式无效")
            phone = ""
            account_key = stored_key
        account = copy.deepcopy(stored_account)
        if (
            not isinstance(account.get("authorization", ""), str)
            or not isinstance(account.get("deviceId", ""), str)
        ):
            raise RuntimeError("移动云盘账号缓存包含无效字段")
        encrypted = account.pop("authorizationEncrypted", None)
        if encrypted is not None:
            if not isinstance(encrypted, dict):
                raise RuntimeError("移动云盘状态文件中的 Authorization 加密数据格式无效")
            encryption_identity = phone if version == 1 else account_key
            account["authorization"] = _decrypt_authorization(
                encryption_identity, encrypted
            )
        account.pop("userDomainId", None)
        account.pop("phone", None)
        account.pop("mobile", None)
        account.pop("phoneNumber", None)
        account.pop("accountLabel", None)
        if account_key in accounts:
            raise RuntimeError("移动云盘状态文件中存在重复账号索引")
        accounts[account_key] = account
    return accounts


def _save_state(accounts: Dict[str, Dict[str, Any]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = STATE_PATH.with_suffix(".tmp")
    try:
        stored_accounts = copy.deepcopy(accounts)
        for account_key, account in stored_accounts.items():
            authorization = account.pop("authorization", "")
            account.pop("authorizationEncrypted", None)
            account.pop("userDomainId", None)
            account.pop("phone", None)
            account.pop("mobile", None)
            account.pop("phoneNumber", None)
            account.pop("accountLabel", None)
            if authorization:
                account["authorizationEncrypted"] = _encrypt_authorization(
                    account_key, authorization
                )
        with temp_path.open("w", encoding="utf-8") as state_file:
            json.dump(
                {"version": 2, "accounts": stored_accounts},
                state_file,
                ensure_ascii=False,
                indent=2,
            )
            state_file.flush()
            os.fsync(state_file.fileno())
        os.replace(temp_path, STATE_PATH)
    except (OSError, RuntimeError) as error:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        if isinstance(error, RuntimeError):
            raise
        raise RuntimeError(f"保存移动云盘状态文件失败：{error}") from error


def _generate_device_profile() -> str:
    device_uuid = str(uuid.uuid4())
    now_ms = int(time.time() * 1000)
    now_cst = datetime.now(timezone(timedelta(hours=8)))
    start_time = now_ms - random.randint(1_800_000, 5_400_000)
    user_agent = USER_AGENT
    rsa_key = RSA.import_key(base64.b64decode(DEVICE_PROFILE_PUBLIC_KEY))
    encrypted_uuid = base64.b64encode(
        PKCS1_v1_5.new(rsa_key).encrypt(device_uuid.encode("utf-8"))
    ).decode("ascii")
    stamp = now_cst.strftime("%Y%m%d%H%M%S")
    md5_uuid = hashlib.md5(device_uuid.encode("utf-8")).hexdigest()
    base = f"{stamp}{md5_uuid}00"
    smid = base + hashlib.md5(f"smsk_web_{base}".encode("utf-8")).hexdigest()[:14] + "0"
    fingerprint = {
        "protocol": 242,
        "organization": DEVICE_PROFILE_ORGANIZATION,
        "appId": "default",
        "os": "web",
        "version": "3.0.0",
        "sdkver": "3.0.0",
        "box": "",
        "rtype": "all",
        "smid": smid,
        "subVersion": "1.0.0",
        "time": now_ms - start_time,
        "cdp": 0,
        "maxTouchPoints": 5,
        "connectionRtt": 0,
        "cpucount": 8,
        "battery": {"charging": 0, "level": round(0.6 + random.random() * 0.35, 2)},
        "dg": "5.0 " + user_agent[len("Mozilla/5.0 "):],
        "gj": "zh-CN",
        "rr": "Google Inc.",
        "sv": "Netscape",
        "qc": "Mozilla",
        "ye": 8,
        "jq": 8,
        "lo": [],
        "bw": "",
        "lr": "Etc/GMT-8",
        "nr": 1,
        "no": 0,
        "br": 1,
        "ra": 0,
        "gt": SCREEN_WIDTH,
        "wy": SCREEN_WIDTH,
        "cj": SCREEN_HEIGHT - random.randint(48, 128),
        "wt": random.randint(100, 180),
        "hu": ["chrome"],
        "documentExist": 1,
        "yi": ["location"],
        "dx": "UTF-8",
        "ig": now_cst.strftime("%a %b %d %Y %H:%M:%S ") + "(GMT+08:00)",
        "ii": 1,
        "fs": 0,
        "ga": 0,
        "tk": 0,
        "rm": 0,
        "kr": 0,
        "nk": 0,
        "by": "srgb",
        "ar": 0,
        "or": 0,
        "et": 0,
        "zc": 0,
        "fj": 0,
        "dc": 0,
        "vd": 0,
        "ni": "",
        "hn": "",
        "hv": "48000_2_1_0_2_explicit_speakers|______",
        "de": md5_uuid[:16] + "|10011011111000111100001100101101111100110101001110000000000100000",
        "xt": 1,
        "vh": 0,
        "xc": {"red": "0"},
        "pm": {
            "default": round(120.5 + random.random() * 20, 1),
            "apple": round(120.5 + random.random() * 20, 1),
            "serif": round(100 + random.random() * 20, 1),
            "sans": round(120.5 + random.random() * 20, 1),
            "mono": round(100 + random.random() * 20, 1),
            "min": round(10 + random.random() * 2, 1),
            "system": round(120.5 + random.random() * 20, 1),
        },
        "ob": {"maxTouchPoints": 5, "touchEvent": True, "touchStart": True},
        "incognito": {
            "getDirectoryExist": 0,
            "getDirectoryIncognito": 0,
            "maxTouchPointsExist": 1,
            "indexedDBIncognito": 0,
            "openDatabaseExist": 0,
            "openDatabaseIncognito": 0,
            "localStorageExist": 1,
            "localStorageIncognito": 0,
            "promiseExist": 1,
            "promiseAllSettledExist": 1,
            "queryUsageAndQuotaIncognito": 0,
            "webkitRequestFileSystemIncognito": 0,
            "serviceWorkerExist": 1,
            "indexedDBExist": 1,
            "browserName": "Chrome",
        },
        "t": now_cst.strftime("%a %b %d %Y %H:%M:%S GMT+0800 (GMT+08:00)"),
        "collectTime": random.randint(50, 130),
    }
    encoded_fingerprint = base64.b64encode(
        json.dumps(fingerprint, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return json.dumps(
        {
            "appId": "default",
            "organization": DEVICE_PROFILE_ORGANIZATION,
            "ep": encrypted_uuid,
            "data": encoded_fingerprint,
            "os": "web",
            "encode": 1,
            "compress": 0,
        },
        separators=(",", ":"),
    )


def _fetch_device_id(
    confirm_request: Optional[RequestConfirmation] = None,
) -> Optional[str]:
    headers = {
        "User-Agent": MARKET_USER_AGENT,
        "Content-Type": "application/json;charset=UTF-8",
        "Origin": MARKET_BASE_URL,
        "Referer": f"{MARKET_BASE_URL}/portal/mobilecloud/index.html?path=newsignin",
    }
    payload = _generate_device_profile()
    if confirm_request:
        confirm_request("POST", DEVICE_PROFILE_URL, headers, None, None, payload)
    try:
        time.sleep(random.uniform(0.5, 1.5))
        response = requests.post(
            DEVICE_PROFILE_URL,
            data=payload,
            headers=headers,
            timeout=15,
        )
        response.raise_for_status()
        result = response.json()
    except (requests.RequestException, ValueError) as error:
        print(f"获取 deviceId 失败：{error}")
        return None
    if result.get("code") == 1100 and result.get("detail", {}).get("deviceId"):
        return f"B{result['detail']['deviceId']}"
    print("设备信息服务未返回 deviceId，将使用本地生成的设备标识")
    return None


class MCloudCheckin:
    def __init__(
        self,
        authorization: str,
        phone: str,
        state: Dict[str, Dict[str, Any]],
        request_confirmation: Optional[RequestConfirmation] = None,
    ):
        self.phone = phone
        self.authorization = _normalize_authorization(authorization)
        self.request_confirmation = request_confirmation
        account_key = _account_state_key(phone)
        self.account_state = state.setdefault(account_key, {})
        if not isinstance(self.account_state, dict):
            raise RuntimeError("移动云盘账号缓存格式无效")
        self.account_state.pop("accountLabel", None)
        self.account_state.pop("userDomainId", None)
        self.user_domain_id = ""
        cached_auth = self.account_state.get("authorization", "")
        cached_expiry = _token_expiry(cached_auth) or int(self.account_state.get("expiresAt") or 0)
        input_expiry = _token_expiry(self.authorization)
        if cached_auth and cached_expiry > input_expiry:
            self.authorization = cached_auth

        self.session = requests.Session()
        self.device_id = self.account_state.get("deviceId") or os.environ.get("YDYP_DEVICE_ID", "").strip()
        if not self.device_id:
            fetched_device_id = _fetch_device_id(self._confirm_request)
            if fetched_device_id is None and self.request_confirmation:
                raise RuntimeError("测试模式下未能获取服务端 deviceId，停止以避免使用不同设备标识继续测试")
            self.device_id = fetched_device_id or uuid.uuid4().hex
        elif not self.device_id.startswith("B"):
            self.device_id = f"B{self.device_id}"

    def _confirm_request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, str]],
        cookies: Optional[Dict[str, str]],
        params: Optional[Dict[str, str]],
        json_data: Any,
    ) -> None:
        if self.request_confirmation:
            self.request_confirmation(method, url, headers, cookies, params, json_data)

    def _request_json(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: Optional[Dict[str, str]] = None,
        cookies: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, str]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        preview_json_data: Optional[Dict[str, Any]] = None,
        retries: int = 3,
        timeout: int = 20,
    ) -> Dict[str, Any]:
        last_error: Optional[Exception] = None
        for attempt in range(retries):
            self._confirm_request(
                method,
                url,
                headers,
                cookies,
                params,
                preview_json_data if preview_json_data is not None else json_data,
            )
            try:
                response = self.session.request(
                    method,
                    url,
                    headers=headers,
                    cookies=cookies,
                    params=params,
                    json=json_data,
                    timeout=timeout,
                )
                response.raise_for_status()
                result = response.json()
                if not isinstance(result, dict):
                    raise RuntimeError("接口返回的数据格式无效")
                return result
            except (requests.RequestException, ValueError, RuntimeError) as error:
                if isinstance(error, requests.HTTPError) and error.response is not None:
                    status_code = error.response.status_code
                    last_error = RuntimeError(f"HTTP {status_code}")
                    if status_code < 500:
                        break
                else:
                    last_error = RuntimeError(type(error).__name__)
                if attempt < retries - 1:
                    time.sleep(attempt + 1)
        endpoint = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1] or "接口"
        raise RuntimeError(f"{endpoint} 请求失败：{last_error}") from last_error

    def _request_text(
        self,
        url: str,
        *,
        method: str,
        headers: Dict[str, str],
        json_data: Dict[str, Any],
        preview_json_data: Optional[Dict[str, Any]] = None,
    ) -> Tuple[int, str]:
        self._confirm_request(
            method,
            url,
            headers,
            None,
            None,
            preview_json_data if preview_json_data is not None else json_data,
        )
        try:
            response = self.session.request(
                method,
                url,
                headers=headers,
                json=json_data,
                timeout=60,
            )
            response.raise_for_status()
        except requests.RequestException as error:
            status_code = error.response.status_code if error.response is not None else "无"
            raise RuntimeError(f"AI 助手请求失败（HTTP {status_code}）") from error
        return response.status_code, response.text

    def _refresh_authorization(self) -> None:
        expiry = _token_expiry(self.authorization) or int(
            self.account_state.get("expiresAt") or 0
        )
        if expiry and expiry > _now_ms() + FIVE_DAYS_MS:
            return
        tid = str(uuid.uuid4())
        encrypted_phone = AES.new(
            REFRESH_TOKEN_AES_KEY.encode("utf-8"), AES.MODE_ECB
        ).encrypt(pad(json.dumps({"phoneNumber": self.phone}, separators=(",", ":")).encode(), AES.block_size))
        headers = {
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "x-yun-tid": tid,
            "Authorization": self.authorization,
            "x-yun-api-version": "v1",
            "x-yun-module-type": "100",
            "x-yun-op-type": "1",
            "x-yun-app-channel": "10214200",
            "x-yun-client-info": "||8||||||||||||",
            "hcy-cool-flag": "1",
        }
        try:
            result = self._request_json(
                REFRESH_TOKEN_URL,
                method="POST",
                headers=headers,
                json_data={"data": base64.b64encode(encrypted_phone).decode("ascii")},
                retries=1,
            )
        except RuntimeError as error:
            print(f"Authorization 自动刷新失败，将尝试当前凭证：{error}")
            return
        code = str(result.get("code", ""))
        data = result.get("data")
        if not (
            result.get("success")
            or code in {"0", "00", "000", "0000"}
            or (code.startswith("0") and len(code) <= 4)
        ) or not isinstance(data, dict) or not data.get("token"):
            print(f"Authorization 自动刷新失败：{result.get('message') or result.get('msg', '未知错误')}")
            return
        self.authorization = _build_authorization(self.phone, data["token"])
        self.account_state["authorization"] = self.authorization
        self.account_state["expiresAt"] = _now_ms() + int(
            float(data.get("expireTime") or TOKEN_EXPIRE_SECONDS_FALLBACK)
        ) * 1000
        print("Authorization 自动刷新成功")

    def _query_spec_token(self, source_id: str) -> str:
        spec_result = self._request_json(
            SPEC_TOKEN_URL,
            method="POST",
            headers={
                "Authorization": self.authorization,
                "User-Agent": USER_AGENT,
                "Content-Type": "application/json",
                "Accept": "*/*",
            },
            json_data={"account": self.phone, "toSourceId": source_id},
        )
        spec_data = spec_result.get("data")
        if (
            not spec_result.get("success")
            or not isinstance(spec_data, dict)
            or not spec_data.get("token")
        ):
            raise RuntimeError(f"获取登录令牌失败：{spec_result.get('message', '未知错误')}")
        return spec_data["token"]

    def _login(self) -> str:
        sso_token = self._query_spec_token("001005")
        self.sso_token = sso_token
        jwt_result = self._request_json(
            JWT_URL,
            method="POST",
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "*/*",
                "Host": "caiyun.feixin.10086.cn:7071",
            },
            params={"ssoToken": sso_token},
        )
        jwt_data = jwt_result.get("result")
        if (
            jwt_result.get("code") != 0
            or not isinstance(jwt_data, dict)
            or not jwt_data.get("token")
        ):
            raise RuntimeError(f"移动云盘登录失败：{jwt_result.get('msg', '未知错误')}")
        return jwt_data["token"]

    @staticmethod
    def _today_signed(result: Dict[str, Any]) -> Optional[bool]:
        status = result.get("todaySignIn")
        if isinstance(status, bool):
            return status
        calendar = result.get("cal")
        for day in calendar if isinstance(calendar, list) else []:
            if isinstance(day, dict) and day.get("t"):
                return bool(day.get("s"))
        return None

    def _market_context(self, jwt_token: str) -> Tuple[Dict[str, str], Dict[str, str]]:
        user_domain_id = ""
        try:
            payload = jwt_token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            decoded = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
            subject = decoded.get("sub", "")
            if isinstance(subject, str):
                subject = json.loads(subject)
            if isinstance(subject, dict):
                user_domain_id = subject.get("userDomainId", "")
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            pass
        self.user_domain_id = (
            user_domain_id if isinstance(user_domain_id, str) else ""
        )
        referer = (
            f"{MARKET_BASE_URL}/portal/mobilecloud/index.html?path=newsignin"
            f"&sourceid=1097&enableShare=1&token={getattr(self, 'sso_token', '')}"
            "&targetSourceId=001005"
        )
        headers = {
            "User-Agent": MARKET_USER_AGENT,
            "Accept": "*/*",
            "jwtToken": jwt_token,
            "X-Requested-With": "com.chinamobile.mcloud",
            "Referer": referer,
            "deviceId": self.device_id,
        }
        cookies = {"jwtToken": jwt_token}
        cookies[f".thumbcache_{self.phone}"] = self.device_id[1:] if self.device_id.startswith("B") else self.device_id
        if user_domain_id:
            cookies["userDomainId"] = user_domain_id
        self.account_state["deviceId"] = self.device_id
        if _token_expiry(self.authorization):
            self.account_state["expiresAt"] = _token_expiry(self.authorization)
        return headers, cookies

    def _market_json(
        self,
        path: str,
        headers: Dict[str, str],
        cookies: Dict[str, str],
        *,
        method: str = "GET",
        params: Optional[Dict[str, str]] = None,
        data: Optional[Dict[str, Any]] = None,
        retries: int = 3,
    ) -> Dict[str, Any]:
        return self._request_json(
            f"{MARKET_BASE_URL}{path}",
            method=method,
            headers=headers,
            cookies=cookies,
            params=params,
            json_data=data,
            retries=retries,
        )

    @staticmethod
    def _task_name(task: Dict[str, Any]) -> str:
        return re.sub(r"<[^>]+>", "", str(task.get("name") or task.get("taskName") or "未命名任务"))

    def _get_cloud_task_list(
        self,
        group: str,
        headers: Dict[str, str],
        cookies: Dict[str, str],
    ) -> List[Dict[str, Any]]:
        tasks = self._get_cloud_task_lists(headers, cookies)
        if group not in tasks:
            raise ValueError(f"未知云朵任务分组：{group}")
        return tasks[group]

    def _get_cloud_task_lists(
        self,
        headers: Dict[str, str],
        cookies: Dict[str, str],
    ) -> Dict[str, List[Dict[str, Any]]]:
        task_headers = {
            **headers,
            "activityId": "sign_in_3",
            "appVersion": APP_VERSION_HEADER,
            "sec-ch-ua-platform": '"Android"',
            "sec-ch-ua-mobile": "?1",
            "sec-ch-ua": (
                '"Not=A?Brand";v="99", "Android WebView";v="151", '
                '"Chromium";v="151"'
            ),
        }
        response = self._market_json(
            "/ycloud/signin/task/taskListV3",
            task_headers,
            cookies,
            method="POST",
            data={
                "marketname": "sign_in_3",
                "client": 0,
                "clientVersion": CLIENT_VERSION,
            },
        )
        if response.get("code") != 0:
            raise RuntimeError(f"获取云朵任务列表失败：{response.get('msg', '未知错误')}")
        result = response.get("result")
        if not isinstance(result, list):
            raise RuntimeError("V3 云朵任务列表格式无效：result 应为数组")
        tasks_by_group = {group: [] for group, _ in CLOUD_TASK_GROUPS}
        for task in result:
            if not isinstance(task, dict):
                raise RuntimeError("V3 云朵任务列表包含无效任务")
            group = task.get("groupid")
            if (
                not isinstance(group, str)
                or group not in tasks_by_group
                or task.get("enable", 1) not in (1, True, "1")
            ):
                continue
            tasks_by_group[group].append(task)
        return tasks_by_group

    def _click_cloud_task(
        self,
        task_id: Any,
        headers: Dict[str, str],
        cookies: Dict[str, str],
        key: str = "task",
    ) -> Dict[str, Any]:
        response = self._market_json(
            "/ycloud/signin/task/click",
            headers,
            cookies,
            params={"key": key, "id": str(task_id)},
            retries=1,
        )
        if response.get("code") != 0:
            raise RuntimeError(response.get("msg", "任务操作失败"))
        return response

    def _client_device_hash(self) -> str:
        device_hash = self.account_state.get("clientDeviceHash")
        if not isinstance(device_hash, str) or not re.fullmatch(r"[0-9A-F]{32}", device_hash):
            device_hash = secrets.token_hex(16).upper()
            self.account_state["clientDeviceHash"] = device_hash
        return device_hash

    def _file_api_headers(self) -> Dict[str, str]:
        device_hash = self._client_device_hash()
        client_version_id = self.account_state.get("clientVersionId")
        if not isinstance(client_version_id, str) or not re.fullmatch(r"[0-9a-f]{16}", client_version_id):
            client_version_id = secrets.token_hex(8)
            self.account_state["clientVersionId"] = client_version_id
        client_info = FILE_API_CLIENT_INFO.format(
            device_hash=device_hash,
            client_version_id=client_version_id,
        )
        headers = {
            "x-yun-api-version": "v1",
            "Connection": "keep-alive",
            "x-yun-net-type": "1",
            "x-yun-client-info": client_info,
            "x-yun-svc-type": "1",
            "x-yun-module-type": "100",
            "x-yun-device-id": client_info,
            "x-yun-user-agent": FILE_API_USER_AGENT,
            "x-yun-app-channel": "10000023",
            "Accept-Language": "zh-CN",
            "x-yun-tid": str(uuid.uuid4()),
            "Authorization": self.authorization,
            "Content-Type": "application/json; charset=UTF-8",
            "User-Agent": "okhttp/4.12.0",
        }
        user_domain_id = self.user_domain_id
        if user_domain_id:
            headers["x-yun-uni"] = str(user_domain_id)
        return headers

    def _create_cloud_file(self, prefix: str) -> Dict[str, Any]:
        now = datetime.now(timezone(timedelta(hours=8)))
        file_name = f"{prefix}{now.strftime('%Y%m%d_%H%M%S_%f')}.txt"
        headers = self._file_api_headers()
        headers["x-yun-op-type"] = "1"
        headers["x-yun-sub-op-type"] = "100"
        # 创建请求超时后服务端可能已落盘，重试会创建重名文件并触发自动改名。
        response = self._request_json(
            "https://personal-kd-njs.yun.139.com/hcy/file/create",
            method="POST",
            headers=headers,
            json_data={
                "contentHash": CLOUD_FILE_HASH,
                "contentHashAlgorithm": "SHA256",
                "contentType": "application/oct-stream",
                "fileRenameMode": "force_rename",
                "localCreatedAt": now.isoformat(timespec="milliseconds"),
                "name": file_name,
                "parallelUpload": True,
                "parentFileId": "/",
                "partInfos": [
                    {"end": len(CLOUD_FILE_CONTENT), "partNumber": 1,
                     "partSize": len(CLOUD_FILE_CONTENT), "start": 0}
                ],
                "size": len(CLOUD_FILE_CONTENT),
                "type": "file",
            },
            retries=1,
        )
        data = response.get("data")
        if not response.get("success") or not isinstance(data, dict) or not data.get("fileId"):
            raise RuntimeError(f"创建云盘临时文件失败：{response.get('message', '未知错误')}")
        return {"fileId": data["fileId"], "fileName": data.get("fileName", file_name)}

    def _trash_cloud_files(self, file_ids: List[str]) -> None:
        if not file_ids:
            return
        response = self._request_json(
            "https://personal-kd-njs.yun.139.com/hcy/recyclebin/batchTrash",
            method="POST",
            headers=self._file_api_headers(),
            json_data={"fileIds": file_ids},
        )
        if not response.get("success"):
            raise RuntimeError(f"清理云盘临时文件失败：{response.get('message', '未知错误')}")

    def _upload_task_file(self) -> None:
        uploaded = self._create_cloud_file("auto_upload_")
        try:
            print(f"-上传临时文件成功：{uploaded['fileName']}")
        finally:
            self._trash_cloud_files([uploaded["fileId"]])
        time.sleep(random.uniform(1, 2))

    def _complete_share_task(self) -> str:
        share_file = self._create_cloud_file("auto_share_")
        try:
            response = self._request_json(
                "https://yun.139.com/orchestration/personalCloud-rebuild/outlink/v1.0/getOutLink",
                method="POST",
                headers={
                    "Authorization": self.authorization,
                    "x-yun-api-version": "v1",
                    "x-yun-app-channel": "10000023",
                    "x-yun-client-info": f"||9|{CLIENT_VERSION}|Chrome|{CHROME_VERSION}|codextestshare||Windows 10||zh-CN|||Q2hyb21l||",
                    "x-yun-module-type": "100",
                    "x-yun-svc-type": "1",
                    "x-SvcType": "1",
                    "Content-Type": "application/json;charset=UTF-8",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/143.0.0.0 Safari/537.36",
                    "Referer": "https://yun.139.com/shareweb/",
                    "Origin": "https://yun.139.com",
                },
                json_data={
                    "getOutLinkReq": {
                        "subLinkType": 0,
                        "encrypt": 1,
                        "coIDLst": [share_file["fileId"]],
                        "caIDLst": [],
                        "pubType": 1,
                        "dedicatedName": share_file["fileName"],
                        "period": 1,
                        "periodUnit": 1,
                        "viewerLst": [],
                        "extInfo": {"isWatermark": 0, "shareChannel": "3001"},
                        "commonAccountInfo": {"account": self.phone, "accountType": 1},
                    }
                },
                retries=1,
            )
        finally:
            self._trash_cloud_files([share_file["fileId"]])
        response_data = response.get("data")
        response_data = response_data if isinstance(response_data, dict) else {}
        result = response_data.get("result") or response_data.get("getOutLinkRes") or {}
        if not isinstance(result, dict):
            result = {}
        outlinks = result.get("getOutLinkResSet") or response_data.get("getOutLinkResSet") or []
        share_succeeded = (
            response.get("success") and str(result.get("resultCode")) == "0"
        ) or (
            str(response.get("code")) in {"0", "0000"} and bool(outlinks)
        )
        if not share_succeeded:
            raise RuntimeError(
                f"创建分享失败：{result.get('resultDesc') or response.get('message', '未知错误')}"
            )

        return "分享链接创建成功（任务进度以客户端展示为准）"

    def _ai_camera_headers(self, *, chat: bool = False) -> Dict[str, str]:
        device_hash = self._client_device_hash()
        headers = {
            "Authorization": self.authorization,
            "x-yun-api-version": "v1",
            "x-yun-tid": str(uuid.uuid4()),
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Origin": "https://frontend.mcloud.139.com",
            "Referer": "https://frontend.mcloud.139.com/",
            "X-Requested-With": "com.chinamobile.mcloud",
            "sec-ch-ua-platform": '"Android"',
            "sec-ch-ua-mobile": "?1",
            "sec-ch-ua": (
                f'"Not=A?Brand";v="99", "Android WebView";v="{CHROME_VERSION.split(".")[0]}", '
                f'"Chromium";v="{CHROME_VERSION.split(".")[0]}"'
            ),
            "Accept-Language": "zh,zh-CN;q=0.9,en-US;q=0.8,en;q=0.7",
        }
        user_domain_id = self.user_domain_id
        if user_domain_id:
            headers["x-yun-uni"] = str(user_domain_id)
        if chat:
            headers["Accept"] = "text/event-stream"
            headers["x-yun-client-info"] = (
                f"4||1|{APP_VERSION}|{DEVICE_BRAND}|{DEVICE_MODEL}|{device_hash}|"
                f"android {ANDROID_VERSION}|||||"
            )
            headers["x-yun-app-channel"] = "101"
        else:
            headers["Accept"] = "*/*"
            headers["x-DeviceInfo"] = (
                f"||36|{APP_VERSION}|{DEVICE_BRAND}|{DEVICE_MODEL}|{device_hash}|"
                f"android {ANDROID_VERSION}|||||"
            )
        return headers

    def _complete_ai_camera_task(self) -> bool:
        user_domain_id = self.user_domain_id
        if not isinstance(user_domain_id, str) or not user_domain_id:
            print("-AI 相机任务未运行：登录响应中没有 userDomainId")
            return False
        image_paths = sorted(
            path
            for path in AI_CAMERA_IMAGE_DIR.iterdir()
            if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg"}
        ) if AI_CAMERA_IMAGE_DIR.is_dir() else []
        if not image_paths:
            raise RuntimeError(
                f"AI 相机图片目录为空或不存在：{AI_CAMERA_IMAGE_DIR}"
            )
        image_path = random.choice(image_paths)
        try:
            image_bytes = image_path.read_bytes()
        except OSError as error:
            raise RuntimeError(f"读取 AI 相机图片失败：{error}") from error
        if not image_bytes:
            raise ValueError("AI 相机图片为空")
        if len(image_bytes) > 5 * 1024 * 1024:
            raise ValueError("AI 相机图片不能超过 5 MiB")

        image_data = "data:image/jpg;base64," + base64.b64encode(image_bytes).decode("ascii")
        recognize_payload = {
            "channelId": "101",
            "userId": user_domain_id,
            "recognizeType": "1",
            "base64": image_data,
            "sendType": "2",
            "imageExt": "jpg",
            "uploadToCloud": True,
            "timeout": 30000,
        }
        recognize_preview = {
            **recognize_payload,
            "base64": f"<本地图片已隐藏：{image_path.name}，{len(image_bytes)} 字节>",
        }
        recognized = self._request_json(
            "https://ai.yun.139.com/api/image/aiRecognize",
            method="POST",
            headers=self._ai_camera_headers(),
            json_data=recognize_payload,
            preview_json_data=recognize_preview,
            retries=1,
            timeout=60,
        )
        if not recognized.get("success"):
            raise RuntimeError(
                f"AI 相机图片识别失败：{recognized.get('message') or recognized.get('msg', '未知错误')}"
            )
        result = recognized.get("data")
        if not isinstance(result, dict) or not result.get("fileId"):
            raise RuntimeError("AI 相机识别响应中缺少文件 ID")

        task_id = str(result.get("taskId") or int(time.time() * 1000))
        file_name = f"{int(task_id) + 1}.jpeg" if task_id.isdigit() else f"{task_id}.jpeg"
        chat_payload = {
            "userId": user_domain_id,
            "sessionId": "",
            "applicationType": "chat",
            "applicationId": "",
            "sourceChannel": "101",
            "dialogueInput": {
                "dialogue": "请识别这张图片。",
                "prompt": "",
                "inputTime": datetime.now(timezone(timedelta(hours=8))).isoformat(
                    timespec="milliseconds"
                ),
                "enableForceLlm": False,
                "enableForceNetworkSearch": False,
                "enableModelThinking": False,
                "enableAllNetworkSearch": False,
                "enableKnowledgeAndNetworkSearch": False,
                "enableRegenerate": False,
                "versionInfo": {"h5Version": "2.7.6"},
                "extInfo": "{}",
                "sortInfo": {},
                "toolSetting": {"imageToolSetting": {"enableLlmDescribe": True}},
                "attachment": {
                    "attachmentTypeList": [3],
                    "fileList": [{"fileId": result["fileId"], "name": file_name}],
                },
            },
        }
        status_code, response_text = self._request_text(
            "https://ai.yun.139.com/api/outer/assistant/chat/v2/add",
            method="POST",
            headers=self._ai_camera_headers(chat=True),
            json_data=chat_payload,
        )
        if status_code != 200:
            raise RuntimeError(f"AI 助手返回 HTTP {status_code}")
        for line in response_text.splitlines():
            if not line.startswith("data:"):
                continue
            event_data = line[5:].strip()
            if not event_data or event_data == "[DONE]":
                continue
            try:
                event = json.loads(event_data)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("success") or str(event.get("code")) == "0000":
                return True
        if response_text.strip():
            try:
                event = json.loads(response_text)
            except json.JSONDecodeError:
                event = {}
            if isinstance(event, dict) and (
                event.get("success") or str(event.get("code")) == "0000"
            ):
                return True
        raise RuntimeError("AI 助手响应中没有成功事件")

    def _handle_cloud_task(
        self,
        group: str,
        task: Dict[str, Any],
        headers: Dict[str, str],
        cookies: Dict[str, str],
    ) -> None:
        task_id = task.get("id")
        name = self._task_name(task)
        if task.get("state") == "FINISH":
            print(f"-已完成：{name}")
            return

        try:
            if group == "day" and task_id == 106:
                self._click_cloud_task(task_id, headers, cookies)
                self._upload_task_file()
                print(f"-已完成上传任务：{name}")
                return
            if task_id == 522:
                target = 100
                progress = int(task.get("process") or 0)
                if progress >= target:
                    print(f"-已完成：{name}")
                    return
                print(f"-月上传任务：当前进度 {progress}/{target}，开始补传")
                for _ in range(target - progress):
                    self._upload_task_file()
                print(f"-月上传任务补传结束：{name}")
                return
            if task_id == 434:
                print(f"-{name}：{self._complete_share_task()}")
                return
            if task_id == 406:
                print(f"-需在客户端手动完成：{name}（通知任务）")
                return
            if task_id == 585:
                step_types = task.get("stepTypeSet")
                try:
                    if (
                        isinstance(step_types, list)
                        and "click" in step_types
                        and int(task.get("currstep") or 0) == 0
                    ):
                        self._click_cloud_task(task_id, headers, cookies)
                    ai_completed = self._complete_ai_camera_task()
                except (RuntimeError, ValueError, TypeError) as error:
                    try:
                        refreshed_tasks = self._get_cloud_task_list(
                            "time", headers, cookies
                        )
                    except (RuntimeError, ValueError) as status_error:
                        print(
                            f"-AI 相机请求失败：{error}；"
                            f"无法确认任务状态：{status_error}"
                        )
                        return
                    refreshed_task = next(
                        (
                            item
                            for item in refreshed_tasks
                            if str(item.get("id")) == str(task_id)
                        ),
                        None,
                    )
                    if refreshed_task and refreshed_task.get("state") == "FINISH":
                        print(f"-已完成：{name}")
                        return
                    current_step = (
                        refreshed_task.get("currstep")
                        if refreshed_task is not None
                        else "未知"
                    )
                    print(
                        f"-AI 相机未完成：{name}；当前步骤标记 {current_step}；"
                        f"请求失败：{error}"
                    )
                    return
                if not ai_completed:
                    print(f"-AI 相机任务未完成：{name}")
                    return
                try:
                    refreshed_tasks = self._get_cloud_task_list(
                        "time", headers, cookies
                    )
                except (RuntimeError, ValueError) as error:
                    print(
                        f"-AI 相机体验/提问请求成功，任务状态暂不可确认：{error}"
                    )
                    return
                refreshed_task = next(
                    (item for item in refreshed_tasks if str(item.get("id")) == str(task_id)),
                    task,
                )
                if refreshed_task.get("state") == "FINISH":
                    print(f"-已完成：{name}")
                else:
                    print(f"-AI 相机体验/提问请求成功，进度以客户端展示为准：{name}")
                return
            if task_id == 478:
                result = self._click_cloud_task(
                    task_id,
                    headers,
                    cookies,
                    key="randomCloudTask",
                ).get("result") or {}
                reward = result.get("num", 0)
                message = result.get("msg", "")
                print(f"-{name}：获得 {reward} 云朵 {message}" if reward else f"-{name}：{message}")
                return

            step_types = task.get("stepTypeSet")
            keys = ["task"]
            if task_id == 409:
                keys = ["task2"] if int(task.get("currstep") or 0) > 0 else ["task", "task2"]
            elif isinstance(step_types, list) and "click" not in step_types:
                print(f"-需手动完成：{name}")
                return
            self._click_cloud_task(task_id, headers, cookies, keys[0])
            if len(keys) > 1:
                self._click_cloud_task(task_id, headers, cookies, keys[1])
            print(f"-已登记任务：{name}")
        except (RuntimeError, ValueError, TypeError) as error:
            print(f"-任务处理失败：{name}：{error}")

    def _claim_revival_reward(
        self,
        headers: Dict[str, str],
        cookies: Dict[str, str],
    ) -> None:
        try:
            result = self._market_json(
                "/ycloud/signin/page/receiveRevivalReward",
                headers,
                cookies,
                method="POST",
                data={},
                retries=2,
            )
            if result.get("code") == 0:
                reward = (result.get("result") or {}).get("rewardClouds", 0)
                print(f"-云朵复活卡：领取 {reward} 云朵" if reward else "-云朵复活卡：暂无奖励")
            else:
                print(f"-云朵复活卡领取失败：{result.get('msg', '未知错误')}")
        except RuntimeError as error:
            print(f"-云朵复活卡领取失败：{error}")

    @staticmethod
    def _cloud_amount(value: Any) -> Optional[int]:
        if value is None or isinstance(value, bool):
            return None
        try:
            amount = int(value)
        except (TypeError, ValueError):
            return None
        return amount if amount >= 0 else None

    def _get_cloud_info(
        self,
        headers: Dict[str, str],
        cookies: Dict[str, str],
    ) -> Dict[str, Any]:
        response = self._market_json(
            "/ycloud/signin/page/infoV3",
            headers,
            cookies,
            params={"client": "app"},
        )
        if response.get("code") != 0:
            raise RuntimeError(
                f"查询云朵余额失败：{response.get('msg', '未知错误')}"
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("云朵余额响应格式无效")
        return result

    def _claim_pending_clouds(
        self,
        headers: Dict[str, str],
        cookies: Dict[str, str],
        initial_total: Optional[int],
    ) -> Tuple[int, Optional[int]]:
        before = self._get_cloud_info(headers, cookies)
        pending_before = self._cloud_amount(before.get("toReceive"))
        if pending_before is None:
            raise RuntimeError("云朵余额响应中缺少有效的待领取数量")
        total_before = self._cloud_amount(before.get("total"))
        received_from_response: Optional[int] = None
        claim_error: Optional[str] = None

        if pending_before:
            receive_headers = {
                **headers,
                "showLoading": "true",
                "appVersion": APP_VERSION_HEADER,
                "activityId": "sign_in_3",
            }
            try:
                response = self._market_json(
                    "/ycloud/signin/page/receiveV3",
                    receive_headers,
                    cookies,
                    method="POST",
                    params={"client": "app"},
                    data={},
                    retries=1,
                )
                if response.get("code") != 0:
                    claim_error = str(response.get("msg", "未知错误"))
                else:
                    result = response.get("result")
                    if isinstance(result, dict):
                        received_from_response = self._cloud_amount(
                            result.get("receive")
                        )
            except RuntimeError as error:
                claim_error = str(error)

        after = self._get_cloud_info(headers, cookies)
        pending_after = self._cloud_amount(after.get("toReceive"))
        total_after = self._cloud_amount(after.get("total"))
        pending_reduced = (
            pending_after is not None and pending_after < pending_before
        )
        total_increased = (
            total_before is not None
            and total_after is not None
            and total_after > total_before
        )
        if pending_before and claim_error and not (pending_reduced or total_increased):
            raise RuntimeError(f"领取待发云朵失败：{claim_error}")
        if (
            pending_before
            and not claim_error
            and not pending_reduced
            and not total_increased
            and not (
                received_from_response is not None
                and received_from_response > 0
            )
        ):
            raise RuntimeError("领取接口返回成功，但无法确认云朵已到账")

        balance_delta = (
            total_after - initial_total
            if initial_total is not None and total_after is not None
            else 0
        )
        if balance_delta > 0:
            received = balance_delta
        elif received_from_response is not None:
            received = received_from_response
        elif pending_reduced and pending_after is not None:
            received = pending_before - pending_after
        else:
            received = 0

        print(
            f"-本次增加云朵：{received}；"
            f"当前云朵总量：{total_after if total_after is not None else '未知'}"
        )
        return received, total_after

    def _log_multiple_cloud_reward(
        self,
        headers: Dict[str, str],
        cookies: Dict[str, str],
    ) -> None:
        try:
            result = self._market_json(
                "/ycloud/signin/page/multiple",
                headers,
                cookies,
                retries=2,
            )
            if result.get("code") != 0:
                return
            cloud_count = int((result.get("result") or {}).get("cloudCount") or 0)
            if cloud_count > 0:
                print(f"-云朵翻倍：可领取 {cloud_count} 云朵")
        except (RuntimeError, TypeError, ValueError) as error:
            print(f"-查询云朵翻倍奖励失败：{error}")

    def _run_cloud_tasks(
        self,
        headers: Dict[str, str],
        cookies: Dict[str, str],
        initial_total: Optional[int],
    ) -> Tuple[int, Optional[int]]:
        tasks_by_group: Optional[Dict[str, List[Dict[str, Any]]]] = None
        try:
            tasks_by_group = self._get_cloud_task_lists(headers, cookies)
        except RuntimeError as error:
            print(f"-获取云朵任务列表失败：{error}")
        if tasks_by_group is not None:
            for group, title in CLOUD_TASK_GROUPS:
                tasks = tasks_by_group[group]
                if not tasks:
                    continue
                print(f"\n✨ {title}")
                for task in tasks:
                    self._handle_cloud_task(group, task, headers, cookies)
        self._claim_revival_reward(headers, cookies)
        self._log_multiple_cloud_reward(headers, cookies)
        return self._claim_pending_clouds(headers, cookies, initial_total)

    def checkin(self) -> str:
        self._refresh_authorization()
        jwt_token = self._login()
        headers, cookies = self._market_context(jwt_token)
        url = f"{MARKET_BASE_URL}/ycloud/signin/page/infoV3"
        status = self._request_json(
            url,
            headers=headers,
            cookies=cookies,
            params={"client": "app"},
        )
        if status.get("code") != 0:
            raise RuntimeError(f"查询签到状态失败：{status.get('msg', '未知错误')}")
        status_data = status.get("result")
        if not isinstance(status_data, dict):
            raise RuntimeError("签到状态响应格式无效")
        initial_total = self._cloud_amount(status_data.get("total"))
        if self._today_signed(status_data):
            signin_outcome = "今天已经签到"
        else:
            time.sleep(random.uniform(1, 2))
            signin_result = self._request_json(
                f"{MARKET_BASE_URL}/ycloud/signin/page/startSignIn",
                headers=headers,
                cookies=cookies,
                params={"client": "app"},
                retries=1,
            )
            if signin_result.get("code") == 0 and self._today_signed(signin_result.get("result") or {}):
                signin_outcome = "签到成功"
            else:
                time.sleep(random.uniform(1, 2))
                latest = self._request_json(
                    url,
                    headers=headers,
                    cookies=cookies,
                    params={"client": "app"},
                )
                if latest.get("code") == 0 and self._today_signed(latest.get("result") or {}):
                    signin_outcome = "签到成功（已复核）"
                else:
                    raise RuntimeError(f"签到失败：{signin_result.get('msg', '签到状态未更新')}")
        cloud_received, cloud_total = self._run_cloud_tasks(
            headers, cookies, initial_total
        )
        received_text = f"；本次增加 {cloud_received} 云朵"
        total_text = (
            f"；总量 {cloud_total} 云朵"
            if cloud_total is not None
            else ""
        )
        return (
            f"{signin_outcome}；云朵任务已执行"
            f"{received_text}{total_text}"
        )


def _split_accounts(raw: str) -> List[Tuple[str, str]]:
    entries = [entry.strip() for entry in re.split(r"[&\n\r;]+", raw or "") if entry.strip()]
    accounts = []
    for entry in entries:
        if "#" not in entry:
            raise ValueError("账号配置格式应为 Authorization#手机号")
        authorization, phone = entry.rsplit("#", 1)
        if not authorization.strip() or not phone.strip():
            raise ValueError("账号配置格式应为 Authorization#手机号")
        accounts.append((authorization.strip(), phone.strip()))
    return accounts


def _mask_phone(phone: str) -> str:
    return f"{phone[:3]}****{phone[-4:]}" if len(phone) >= 11 else phone


def main() -> int:
    raw_accounts = os.environ.get(
        "MCLOUD_COOKIES",
        LOCAL_CONFIG.get("MCLOUD_COOKIES", ""),
    ).strip()
    try:
        accounts = _split_accounts(raw_accounts)
    except ValueError as error:
        print(f"❌ {error}")
        return 1
    if not accounts:
        print("❌ 未配置 MCLOUD_COOKIES")
        return 1

    try:
        state = _load_state()
    except RuntimeError as error:
        print(f"❌ {error}")
        return 1
    results = []
    failures = []
    for index, (authorization, phone) in enumerate(accounts, start=1):
        label = _mask_phone(phone)
        print(f"======== 第 {index}/{len(accounts)} 个账号：{label} ========")
        try:
            client = MCloudCheckin(authorization, phone, state)
            outcome = client.checkin()
            print(f"✅ {label}：{outcome}")
            results.append(f"✅ {label}：{outcome}")
        except (RuntimeError, ValueError, requests.RequestException) as error:
            message = f"❌ {label}：{error}"
            print(message)
            results.append(message)
            failures.append(message)
        try:
            _save_state(state)
        except RuntimeError as error:
            message = f"❌ {label}：移动云盘状态加密/保存失败：{error}"
            print(message)
            results.append(message)
            failures.append(message)
        if index < len(accounts):
            time.sleep(random.uniform(2, 4))

    token = os.environ.get(
        "PUSHPLUS_TOKEN",
        LOCAL_CONFIG.get("PUSHPLUS_TOKEN", ""),
    ).strip()
    if token:
        channel = "cmcc" if failures else "wechat"
        title = "移动云盘签到失败" if failures else "移动云盘签到结果"
        try:
            sent_channel = send_notification(
                title,
                "\n".join(results),
                token,
                channel=channel,
                template="txt",
            )
            print(f"✅ PushPlus 通知已通过 {sent_channel} 渠道提交")
        except (RuntimeError, ValueError) as error:
            print(f"❌ 通知失败：{error}")
            failures.append(str(error))

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
