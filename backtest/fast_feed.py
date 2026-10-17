"""离线 DataFrame 行情的整列预加载。

Backtrader 自带的 PandasData 对每个单元格做 iloc，会在单核上持有 GIL。
本模块只改变装载方式，不改变 data.p.dataname、日期窗口和行情线数值。
"""

import numpy as np
import pandas as pd
import backtrader as bt


_UNIX_ORDINAL = 719163.0
_TICKS_PER_DAY = {
    "s": 86_400,
    "ms": 86_400_000,
    "us": 86_400_000_000,
    "ns": 86_400_000_000_000,
}


def _datetime_unit(index):
    """读取 DatetimeIndex 的实际精度，不能把 asi8 固定当成纳秒。"""
    dtype = getattr(index, "dtype", None)
    unit = getattr(dtype, "unit", None) or getattr(index, "unit", None)
    if unit:
        return str(unit)
    text = str(dtype or "")
    if "[" in text and "]" in text:
        return text.split("[", 1)[1].split("]", 1)[0]
    return "ns"


def _datetime_nums(index):
    """把 naive DatetimeIndex 转成与 backtrader.date2num 一致的浮点日。"""
    if not isinstance(index, pd.DatetimeIndex) or index.tz is not None or index.hasnans:
        return None
    if not index.is_monotonic_increasing:
        return None
    ticks = _TICKS_PER_DAY.get(_datetime_unit(index))
    if ticks is None:
        return None
    values = np.asarray(index.asi8, dtype=np.int64)
    days = np.floor_divide(values, ticks)
    remainder = np.remainder(values, ticks)
    return _UNIX_ORDINAL + days.astype(np.float64) + remainder.astype(np.float64) / float(ticks)


def _numeric_column(frame, column_index):
    """取出一列 float64。无法无损转换时返回 None，调用方回退逐行加载。"""
    try:
        values = frame.iloc[:, column_index].to_numpy(copy=False)
        numeric = np.asarray(values, dtype=np.float64)
    except (TypeError, ValueError):
        return None
    if numeric.shape != (len(frame),):
        return None
    if numeric.dtype != np.float64:
        return None
    return numeric


class FastPandasData(bt.feeds.PandasData):
    """DataFrame 行情。预加载整列写入，语义与 PandasData 的日期窗口一致。"""

    def preload(self):
        if not self._try_bulk_preload():
            super().preload()

    def _try_bulk_preload(self):
        prepared = self._prepare_columns()
        if prepared is None:
            return False
        aliases, columns, length = prepared
        lines = [getattr(self.lines, alias) for alias in aliases]
        if any(
            getattr(line, "mode", None) != line.UnBounded or len(line.array) or line.extension
            for line in lines
        ):
            return False
        try:
            for line, values in zip(lines, columns):
                if length:
                    block = np.ascontiguousarray(values, dtype=np.float64)
                    line.array.frombytes(block.tobytes())
        except Exception:
            for line in lines:
                line.reset()
            return False
        self._last()
        self.home()
        return True

    def _prepare_columns(self):
        frame = getattr(self.p, "dataname", None)
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return None
        if getattr(self, "_filters", None) or getattr(self, "_ffilters", None) or getattr(self, "_tzinput", None):
            return None
        dtnums = _datetime_nums(frame.index)
        if dtnums is None or len(dtnums) != len(frame):
            return None
        start = 0
        end = len(dtnums)
        if self.fromdate != float("-inf"):
            start = int(np.searchsorted(dtnums, self.fromdate, side="left"))
        if self.todate != float("inf"):
            end = int(np.searchsorted(dtnums, self.todate, side="right"))
        if end < start:
            end = start
        aliases = list(self.getlinealiases())
        columns = []
        width = end - start
        for alias in aliases:
            if alias == "datetime":
                columns.append(dtnums[start:end])
                continue
            column_index = self._colmapping.get(alias)
            if column_index is None:
                columns.append(np.full(width, np.nan, dtype=np.float64))
                continue
            if not isinstance(column_index, (int, np.integer)) or int(column_index) < 0:
                return None
            numeric = _numeric_column(frame, int(column_index))
            if numeric is None:
                return None
            columns.append(numeric[start:end])
        return aliases, columns, width
