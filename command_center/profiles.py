"""命令方案的本地保存、加载和删除。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class SavedCommandProfile:
    """不包含环境变量凭据的可复用命令方案。"""

    profile_id: str
    name: str
    updated_at: str
    strategy: str
    selection: str
    data_source: str
    connect: str
    market: str
    mode: str
    shell: str
    system: str
    config_profile: str
    params: dict[str, Any]
    options: dict[str, Any]
    config_override: dict[str, Any]
    origin: str = "用户方案"
    # 可选外部策略仓库根目录；旧方案 JSON 缺失时回退为空。
    source_root: str = ""


def _profile_id(name: str) -> str:
    """生成稳定的文件名安全方案 ID。"""

    slug = re.sub(r"[^0-9A-Za-z_-]+", "_", name).strip("_") or "profile"
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:10]
    return f"{slug[:60]}_{digest}"


class CommandProfileStore:
    """在 .data/command_center 中保存用户命令方案。"""

    def __init__(self, project_root: Path | str) -> None:
        self.path = Path(project_root) / ".data" / "command_center" / "command_profiles.json"
        self.private_credentials_path = self.path.with_name("private_credentials.json")

    def _read_private_credentials(self) -> dict[str, Any]:
        """读取仅供本机工作台使用的私有凭据文件。"""
        try:
            payload = json.loads(self.private_credentials_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def get_private_credentials(self, profile_id: str) -> dict[str, str]:
        """按方案 ID 读取 Futu 私有解锁凭据，不返回到方案列表。"""
        raw = self._read_private_credentials().get(str(profile_id), {})
        if not isinstance(raw, dict):
            return {}
        return {
            key: str(raw.get(key) or "")
            for key in ("FUTU_TRADE_PASSWORD", "FUTU_TRADE_PASSWORD_MD5")
            if str(raw.get(key) or "")
        }

    def set_private_credentials(self, profile_id: str, credentials: Mapping[str, Any]) -> None:
        """保存本机私有 Futu 解锁凭据；该文件位于已忽略的 .data 目录。"""
        payload = self._read_private_credentials()
        values = {
            key: str(credentials.get(key) or "")
            for key in ("FUTU_TRADE_PASSWORD", "FUTU_TRADE_PASSWORD_MD5")
            if str(credentials.get(key) or "")
        }
        if values:
            payload[str(profile_id)] = values
        else:
            payload.pop(str(profile_id), None)
        self.private_credentials_path.parent.mkdir(parents=True, exist_ok=True)
        self.private_credentials_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def delete_private_credentials(self, profile_id: str) -> None:
        """删除方案关联的本机私有解锁凭据。"""
        payload = self._read_private_credentials()
        if str(profile_id) not in payload:
            return
        payload.pop(str(profile_id), None)
        self.private_credentials_path.parent.mkdir(parents=True, exist_ok=True)
        self.private_credentials_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _read(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def list(self) -> list[SavedCommandProfile]:
        """读取所有方案，按更新时间倒序排列。"""

        raw_profiles = self._read().get("profiles", {})
        if not isinstance(raw_profiles, dict):
            return []
        profiles: list[SavedCommandProfile] = []
        for profile_id, raw in raw_profiles.items():
            if not isinstance(raw, dict):
                continue
            try:
                profiles.append(
                    SavedCommandProfile(
                        profile_id=str(raw.get("profile_id", profile_id)),
                        name=str(raw["name"]),
                        updated_at=str(raw.get("updated_at", "")),
                        strategy=str(raw.get("strategy", "")),
                        selection=str(raw.get("selection", "")),
                        data_source=str(raw.get("data_source", "")),
                        connect=str(raw.get("connect", "")),
                        market=str(raw.get("market", "自定义")),
                        mode=str(raw.get("mode", "backtest")),
                        shell=str(raw.get("shell", "")),
                        system=str(raw.get("system", "")),
                        config_profile=str(raw.get("config_profile", "none")),
                        params=dict(raw.get("params", {})),
                        options=dict(raw.get("options", {})),
                        config_override=dict(raw.get("config_override", {})),
                        origin=str(raw.get("origin", "用户方案")),
                        source_root=str(raw.get("source_root", "")),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return sorted(profiles, key=lambda item: item.updated_at, reverse=True)

    def save(self, profile: SavedCommandProfile) -> SavedCommandProfile:
        """保存或覆盖同名方案。"""

        payload = self._read()
        raw_profiles = payload.get("profiles", {})
        profiles = dict(raw_profiles) if isinstance(raw_profiles, dict) else {}
        profiles[profile.profile_id] = asdict(profile)
        payload.update(
            {
                "profiles": profiles,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return profile

    def delete(self, profile_id: str) -> bool:
        """删除一个方案。"""

        payload = self._read()
        raw_profiles = payload.get("profiles", {})
        if not isinstance(raw_profiles, dict) or profile_id not in raw_profiles:
            return False
        profiles = dict(raw_profiles)
        del profiles[profile_id]
        payload["profiles"] = profiles
        payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self.delete_private_credentials(profile_id)
        return True

    def make_profile(self, name: str, **kwargs: Any) -> SavedCommandProfile:
        """根据当前界面字段创建方案对象。"""

        now = datetime.now().isoformat(timespec="seconds")
        return SavedCommandProfile(
            profile_id=_profile_id(name),
            name=name,
            updated_at=now,
            strategy=str(kwargs.get("strategy", "")),
            selection=str(kwargs.get("selection", "")),
            data_source=str(kwargs.get("data_source", "")),
            connect=str(kwargs.get("connect", "")),
            market=str(kwargs.get("market", "自定义")),
            mode=str(kwargs.get("mode", "backtest")),
            shell=str(kwargs.get("shell", "")),
            system=str(kwargs.get("system", "")),
            config_profile=str(kwargs.get("config_profile", "none")),
            params=dict(kwargs.get("params", {})),
            options=dict(kwargs.get("options", {})),
            config_override=dict(kwargs.get("config_override", {})),
            origin=str(kwargs.get("origin", "用户方案")),
            source_root=str(kwargs.get("source_root", "")),
        )
