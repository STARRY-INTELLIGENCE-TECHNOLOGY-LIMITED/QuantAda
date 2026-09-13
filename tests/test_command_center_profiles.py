"""命令方案保存与加载测试。"""

from pathlib import Path

from command_center.profiles import CommandProfileStore


def test_profile_store_saves_named_command_without_variables(tmp_path: Path) -> None:
    store = CommandProfileStore(tmp_path)
    profile = store.make_profile(
        "A股研究方案",
        strategy="strategies.demo",
        selection="selectors.demo",
        data_source="gm",
        mode="backtest",
        params={"lookback": 20},
        options={"desc": "A股研究方案", "no_plot": True},
        config_override={"LOG": False},
    )
    store.save(profile)

    loaded = store.list()
    assert len(loaded) == 1
    assert loaded[0].name == "A股研究方案"
    assert loaded[0].market == "自定义"
    assert loaded[0].params == {"lookback": 20}
    assert loaded[0].config_override == {"LOG": False}
    assert loaded[0].origin == "用户方案"
    assert not hasattr(loaded[0], "variables")


def test_profile_store_same_name_updates_existing_profile(tmp_path: Path) -> None:
    store = CommandProfileStore(tmp_path)
    first = store.make_profile("demo", params={"value": 1})
    second = store.make_profile("demo", params={"value": 2})
    store.save(first)
    store.save(second)

    loaded = store.list()
    assert len(loaded) == 1
    assert loaded[0].params == {"value": 2}
    assert store.delete(loaded[0].profile_id)
    assert store.list() == []


def test_profile_store_can_clear_private_credentials_on_update(tmp_path: Path) -> None:
    from command_center.web import CommandCenterService

    service = CommandCenterService(tmp_path)
    saved = service.save_profile({
        "name": "futu",
        "strategy": "strategies.demo",
        "data_source": "futu",
        "mode": "backtest",
        "variables": {"FUTU_TRADE_PASSWORD": "secret"},
    })
    service.save_profile({
        "name": "futu",
        "save_profile_id": saved["profile_id"],
        "strategy": "strategies.demo",
        "clear_private_credentials": True,
    })
    assert service.profile_store.get_private_credentials(saved["profile_id"]) == {}


def test_profile_store_distinguishes_chinese_names(tmp_path: Path) -> None:
    store = CommandProfileStore(tmp_path)
    store.save(store.make_profile("A股方案"))
    store.save(store.make_profile("全球方案"))

    assert {item.name for item in store.list()} == {"A股方案", "全球方案"}


def test_profile_store_keeps_private_trade_credentials_out_of_profile_listing(tmp_path: Path) -> None:
    store = CommandProfileStore(tmp_path)
    profile = store.make_profile("Futu 实盘方案", strategy="strategies.demo")
    store.save(profile)
    store.set_private_credentials(profile.profile_id, {"FUTU_TRADE_PASSWORD": "fixture"})

    assert store.get_private_credentials(profile.profile_id) == {
        "FUTU_TRADE_PASSWORD": "fixture",
    }
    assert "fixture" not in repr(store.list())
    assert "fixture" not in store.path.read_text(encoding="utf-8")
