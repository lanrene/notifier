from pathlib import Path
from typing import AbstractSet, Dict


LOCAL_CONFIG_PATH = Path(__file__).resolve().parent / ".epic.local.env"


def load_local_config(allowed_keys: AbstractSet[str]) -> Dict[str, str]:
    """从项目本地配置文件中读取指定的键值设置。"""
    if not LOCAL_CONFIG_PATH.exists():
        return {}

    config = {}
    try:
        with LOCAL_CONFIG_PATH.open("r", encoding="utf-8") as config_file:
            for line_number, raw_line in enumerate(config_file, start=1):
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    raise ValueError(
                        f"{LOCAL_CONFIG_PATH} 第{line_number}行缺少 '='"
                    )
                key, value = line.split("=", maxsplit=1)
                key = key.strip()
                if key not in allowed_keys:
                    continue
                value = value.strip()
                if (
                    len(value) >= 2
                    and value[0] == value[-1]
                    and value[0] in {"'", '"'}
                ):
                    value = value[1:-1]
                config[key] = value
    except OSError as e:
        raise RuntimeError(f"读取本地配置文件失败：{e}") from e
    return config
