"""训练批次的不可变行情快照；只保存数据与切分，不保存 Provider 或执行器对象。"""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import pickle
import re
import tempfile
import uuid


SNAPSHOT_VERSION = 1
DATA_FIELDS = (
    "raw_datas", "train_datas", "test_datas", "target_symbols", "source_symbols",
    "train_range", "test_range", "warmup_days", "raw_data_fetch_range",
)


def _sha256_stream(stream):
    """流式计算 SHA-256，兼容 CI 使用的 Python 3.10。"""
    digest = hashlib.sha256()
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def save_training_snapshot(journal, context, args, original_argv, runtime_config, original_exact=True):
    """持锁调用；流式写入后提交清单，同一批次的所有指标共用一次快照。"""
    directory = Path(str(journal) + ".snapshots").resolve()
    directory.mkdir(parents=True, exist_ok=True)
    snapshot_id = uuid.uuid4().hex
    payload = {key: context.get(key) for key in DATA_FIELDS}
    payload["runtime_config"] = runtime_config
    payload["snapshot_frozen"] = True
    with tempfile.TemporaryDirectory(prefix="pending_", dir=directory) as temporary:
        staging = Path(temporary).resolve()
        assert staging.is_relative_to(directory)
        data_file = staging / "data.pkl"
        with data_file.open("wb") as stream:
            pickle.dump(payload, stream, protocol=pickle.HIGHEST_PROTOCOL)
            stream.flush()
            os.fsync(stream.fileno())
        with data_file.open("rb") as stream:
            data_digest = _sha256_stream(stream)
        frames = context.get("raw_datas") or {}
        coverage = {}
        for symbol, frame in frames.items():
            index = frame.index
            coverage[str(symbol)] = {
                "rows": len(frame), "timezone": str(getattr(index, "tz", None)),
                "first": str(index.min()) if len(index) else None,
                "last": str(index.max()) if len(index) else None,
            }
        manifest = {
            "version": SNAPSHOT_VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
            "data_sha256": data_digest, "original_argv": list(original_argv),
            "original_exact": original_exact,
            "start_date": args.start_date, "end_date": args.end_date,
            "timeframe": getattr(args, "timeframe", None), "compression": getattr(args, "compression", None),
            "train_range": context.get("train_range"), "test_range": context.get("test_range"),
            "warmup_days": context.get("warmup_days"), "raw_data_fetch_range": context.get("raw_data_fetch_range"),
            "source_symbols": context.get("source_symbols"), "target_symbols": context.get("target_symbols"),
            "coverage": coverage,
        }
        manifest_bytes = json.dumps(manifest, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        with (staging / "manifest.json").open("wb") as stream:
            stream.write(manifest_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staging, directory / snapshot_id)
    return {
        "id": snapshot_id, "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "journal": str(Path(journal).resolve()),
    }



def _snapshot_directory(journal, reference):
    """优先用当前 Journal 旁的快照；同目录批次锚点只作为回退，不接受任意外部路径。"""
    primary = Path(str(journal) + ".snapshots").resolve()
    hinted = reference.get("journal") if isinstance(reference, dict) else None
    if hinted:
        hinted_dir = Path(str(hinted) + ".snapshots").resolve()
        if hinted_dir != primary and hinted_dir.parent == primary.parent and (hinted_dir / reference["id"]).is_dir():
            if not (primary / reference["id"]).is_dir():
                return hinted_dir
    return primary


def load_training_snapshot(journal, reference):
    """先校验清单与数据指纹，再读取本机快照；缺失或损坏时由编排层重新准备数据。"""
    if not isinstance(reference, dict) or not re.fullmatch(r"[0-9a-f]{32}", str(reference.get("id", ""))):
        raise ValueError("训练数据快照引用无效")
    directory = _snapshot_directory(journal, reference)
    snapshot = (directory / reference["id"]).resolve()
    if not snapshot.is_relative_to(directory):
        raise ValueError("训练数据快照路径越界")
    manifest_bytes = (snapshot / "manifest.json").read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != reference.get("sha256"):
        raise ValueError("训练数据快照清单校验失败")
    manifest = json.loads(manifest_bytes)
    if manifest.get("version") != SNAPSHOT_VERSION:
        raise ValueError("训练数据快照版本不兼容")
    with (snapshot / "data.pkl").open("rb") as stream:
        if _sha256_stream(stream) != manifest.get("data_sha256"):
            raise ValueError("训练数据快照行情校验失败")
        stream.seek(0)
        try:
            payload = pickle.load(stream)
        except (pickle.UnpicklingError, AttributeError, ImportError, TypeError) as exc:
            raise ValueError(f"训练数据快照无法解码：{exc}") from exc
    if not isinstance(payload, dict) or not all(isinstance(payload.get(key), dict) for key in ("raw_datas", "train_datas", "test_datas", "runtime_config")):
        raise ValueError("训练数据快照缺少完整数据")
    if not isinstance(manifest.get("original_argv"), list):
        raise ValueError("训练数据快照缺少原始命令")
    return payload, manifest
