# Android 版本、设备型号和 Build 编号必须对应同一设备配置。
# 此版本号来自本次 taskListV3 请求样例；更换设备时应一起更新。
ANDROID_VERSION = "16"
DEVICE_MODEL = "23127PN0CC"
BUILD_ID = "BP2A.250605.031.A3"
DEVICE_BRAND = "Xiaomi"

# 请求 UA 中声明的 Chromium/WebView 和 MCloudApp 版本。
# 只有核实要模拟的应用或浏览器版本后才需要修改。
CHROME_VERSION = "151.0.7922.199"
APP_VERSION = "13.2.4"
APP_VERSION_HEADER = "13.2.4.0"

# 设备信息请求上报的屏幕像素尺寸；仅在切换到不同分辨率设备时修改。
SCREEN_WIDTH = 1200
SCREEN_HEIGHT = 2536

CLIENT_DEVICE_TYPE = "1"
CLIENT_DEVICE_SOURCE = "127.0.0.1"
CLIENT_NETWORK_TYPE = "1"
CLIENT_LANGUAGE = "zh"
CLIENT_BUILD_CHANNEL = "032"
CLIENT_DEVICE_MAC = "02-00-00-00-00-00"
CLIENT_INFO_EXTRA = "0"

# 云盘 API 请求和设备指纹共用的当前设备 UA。
USER_AGENT = (
    f"Mozilla/5.0 (Linux; Android {ANDROID_VERSION}; {DEVICE_MODEL} Build/{BUILD_ID}; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
    f"Chrome/{CHROME_VERSION} Mobile Safari/537.36 "
    f"MCloudApp/{APP_VERSION} AppLanguage/zh-CN"
)

FILE_API_CLIENT_INFO = (
    f"{CLIENT_DEVICE_TYPE}|{CLIENT_DEVICE_SOURCE}|{CLIENT_NETWORK_TYPE}|{APP_VERSION}|"
    f"{DEVICE_BRAND}|{DEVICE_MODEL}|{{device_hash}}|{CLIENT_DEVICE_MAC}|"
    f"android {ANDROID_VERSION}|{SCREEN_WIDTH}X{SCREEN_HEIGHT}|{CLIENT_LANGUAGE}||||"
    f"{CLIENT_BUILD_CHANNEL}|{CLIENT_INFO_EXTRA}|{{client_version_id}}|"
)

FILE_API_USER_AGENT = (
    f"android|{DEVICE_MODEL}|android {ANDROID_VERSION}|mCloud{APP_VERSION}-{CLIENT_BUILD_CHANNEL}"
)
