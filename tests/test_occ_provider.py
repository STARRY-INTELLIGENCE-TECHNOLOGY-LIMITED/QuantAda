import json

import pandas as pd

from data_providers.occ_provider import OccDataProvider


def test_occ_provider_reads_local_adjustment_archive(tmp_path, monkeypatch):
    import config

    archive = tmp_path / "occ.json"
    archive.write_text(json.dumps({
        "records": [{
            "option_symbol": "US.AAPL240119C00100000",
            "underlying": "AAPL",
            "effective_date": "2024-01-02",
            "announcement_date": "2023-12-29",
            "adjustment_type": "SPECIAL_DIVIDEND",
            "old_multiplier": 100,
            "new_multiplier": 100,
            "old_strike": 100,
            "new_strike": 99.5,
            "source_id": "OCC-TEST-1",
        }]
    }), encoding="utf-8")
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    monkeypatch.setattr(config, "OCC_ACTIONS_PATH", str(archive))

    provider = OccDataProvider()
    metadata = provider.get_option_contract_metadata(
        "US.AAPL240119C00100000", as_of="2024-01-03",
    )

    assert metadata["status"] == "adjusted"
    assert metadata["new_strike"] == 99.5
    assert metadata["source_id"] == "OCC-TEST-1"


def test_occ_provider_unknown_archive_is_explicitly_unknown(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    monkeypatch.setattr(config, "OCC_ACTIONS_PATH", "")
    monkeypatch.setattr(config, "OCC_ACTIONS_URL", "")

    metadata = OccDataProvider().get_option_contract_metadata("US.AAPL240119P00100000")

    assert metadata["status"] == "unknown"
    assert metadata["coverage_complete"] is False
