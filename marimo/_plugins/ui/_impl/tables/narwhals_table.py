# Copyright 2024 Marimo. All rights reserved.
from __future__ import annotations

import functools
import io
from functools import cached_property
from typing import Any, Optional, Union, cast

import narwhals.stable.v1 as nw
from narwhals.stable.v1.typing import IntoFrameT

from marimo import _loggers
from marimo._data.models import ColumnSummary, ExternalDataType
from marimo._dependencies.dependencies import DependencyManager
from marimo._output.data.data import sanitize_json_bigint
from marimo._plugins.core.media import io_to_data_url
from marimo._plugins.ui._impl.tables.format import (
    FormatMapping,
    format_value,
)
from marimo._plugins.ui._impl.tables.selection import INDEX_COLUMN_NAME
from marimo._plugins.ui._impl.tables.table_manager import (
    ColumnName,
    FieldType,
    TableCell,
    TableCoordinate,
    TableManager,
)
from marimo._utils.narwhals_utils import (
    can_narwhalify,
    dataframe_to_csv,
    is_narwhals_integer_type,
    is_narwhals_string_type,
    is_narwhals_temporal_type,
    is_narwhals_time_type,
    unwrap_py_scalar,
)

LOGGER = _loggers.marimo_logger()


class NarwhalsTableManager(
    TableManager[Union[nw.DataFrame[IntoFrameT], nw.LazyFrame[IntoFrameT]]]
):
    type = "narwhals"

    @staticmethod
    def from_dataframe(data: IntoFrameT) -> NarwhalsTableManager[IntoFrameT]:
        return NarwhalsTableManager(nw.from_native(data, strict=True))

    def as_frame(self) -> nw.DataFrame[Any]:
        if isinstance(self.data, nw.LazyFrame):
            return self.data.collect()
        return self.data

    def with_new_data(
        self, data: nw.DataFrame[Any] | nw.LazyFrame[Any]
    ) -> TableManager[Any]:
        if type(self) is NarwhalsTableManager:
            return NarwhalsTableManager(data)
        # If this call comes from a subclass, we need to call the constructor
        # of the subclass with the native data.
        return self.__class__(data.to_native())

    def to_csv_str(
        self,
        format_mapping: Optional[FormatMapping] = None,
    ) -> str:
        _data = self.apply_formatting(format_mapping).as_frame()
        return dataframe_to_csv(_data)

    def to_json_str(
        self, format_mapping: Optional[FormatMapping] = None
    ) -> str:
        try:
            csv_str = self.to_csv_str(format_mapping=format_mapping)
        except Exception as e:
            LOGGER.debug(
                f"Failed to use format mapping: {str(e)}, falling back to default"
            )
            csv_str = self.to_csv_str()

        import csv

        csv_reader = csv.DictReader(csv_str.splitlines())
        return sanitize_json_bigint([row for row in csv_reader])

    def to_parquet(self) -> bytes:
        stream = io.BytesIO()
        self.as_frame().write_parquet(stream)
        return stream.getvalue()

    def apply_formatting(
        self, format_mapping: Optional[FormatMapping]
    ) -> NarwhalsTableManager[Any]:
        if not format_mapping:
            return self

        _data = self.as_frame().to_dict(as_series=False).copy()
        for col in _data.keys():
            if col in format_mapping:
                _data[col] = [
                    format_value(col, x, format_mapping) for x in _data[col]
                ]
        return NarwhalsTableManager(
            nw.from_dict(
                _data, native_namespace=nw.get_native_namespace(self.data)
            )
        )

    def supports_filters(self) -> bool:
        return True

    def select_rows(self, indices: list[int]) -> TableManager[Any]:
        if not indices:
            return self.with_new_data(self.data.head(0))

        df = self.as_frame()
        # Prefer the index column for selections
        if INDEX_COLUMN_NAME in df.columns:
            # Drop the index column before returning
            return self.with_new_data(
                df.filter(nw.col(INDEX_COLUMN_NAME).is_in(indices))
            )
        return self.with_new_data(df[indices])

    def select_columns(self, columns: list[str]) -> TableManager[Any]:
        return self.with_new_data(self.data.select(columns))

    def select_cells(self, cells: list[TableCoordinate]) -> list[TableCell]:
        if not cells:
            return []

        df = self.as_frame()
        if INDEX_COLUMN_NAME in df.columns:
            selection: list[TableCell] = []
            for row, col in cells:
                filtered: nw.DataFrame[Any] = df.filter(
                    nw.col(INDEX_COLUMN_NAME) == int(row)
                )
                if filtered.is_empty():
                    continue

                selection.append(
                    TableCell(row, col, filtered.get_column(col)[0])
                )

            return selection
        else:
            return [
                TableCell(row, col, df.item(row=int(row), column=col))
                for row, col in cells
            ]

    def drop_columns(self, columns: list[str]) -> TableManager[Any]:
        return self.with_new_data(self.data.drop(columns, strict=False))

    def get_row_headers(
        self,
    ) -> list[str]:
        return []

    @functools.lru_cache(maxsize=5)  # noqa: B019
    def calculate_top_k_rows(
        self, column: ColumnName, k: int
    ) -> list[tuple[Any, int]]:
        if isinstance(self.data, nw.LazyFrame):
            raise ValueError(
                "Cannot calculate top k rows for lazy frames, please collect the data first"
            )

        columns = self.get_column_names()

        if column not in columns:
            raise ValueError(f"Column {column} not found in table.")

        # Find a column name for the count that doesn't conflict with existing columns
        chosen_column_name: str | None = None
        for col in ["count", f"count of {column}", "num_rows"]:
            if col not in columns:
                chosen_column_name = col
                break
        if chosen_column_name is None:
            raise ValueError(
                "Cannot specify a count column name, please rename your column"
            )

        # column is also sorted to ensure nulls are last
        result = (
            self.data.group_by(column)
            .agg(nw.len().alias(chosen_column_name))
            .sort(
                [chosen_column_name, column], descending=True, nulls_last=True
            )
            .head(k)
        )

        return [
            (
                unwrap_py_scalar(row[column]),
                int(unwrap_py_scalar(row[chosen_column_name])),
            )
            for row in result.iter_rows(named=True)
        ]

    @staticmethod
    def is_type(value: Any) -> bool:
        return can_narwhalify(value)

    @cached_property
    def nw_schema(self) -> nw.Schema:
        return cast(nw.Schema, self.data.collect_schema())

    def get_field_type(
        self, column_name: str
    ) -> tuple[FieldType, ExternalDataType]:
        dtype = self.nw_schema[column_name]
        dtype_string = str(dtype)
        if is_narwhals_string_type(dtype):
            return ("string", dtype_string)
        elif dtype == nw.Boolean:
            return ("boolean", dtype_string)
        elif dtype == nw.Duration:
            return ("number", dtype_string)
        elif dtype.is_integer():
            return ("integer", dtype_string)
        elif is_narwhals_time_type(dtype):
            return ("time", dtype_string)
        elif dtype == nw.Date:
            return ("date", dtype_string)
        elif dtype == nw.Datetime:
            return ("datetime", dtype_string)
        elif dtype.is_temporal():
            return ("datetime", dtype_string)
        elif dtype.is_numeric():
            return ("number", dtype_string)
        else:
            return ("unknown", dtype_string)

    def take(self, count: int, offset: int) -> TableManager[Any]:
        if count < 0:
            raise ValueError("Count must be a positive integer")
        if offset < 0:
            raise ValueError("Offset must be a non-negative integer")

        if offset == 0:
            return self.with_new_data(self.data.head(count))
        else:
            return self.with_new_data(self.data[offset : offset + count])

    def search(self, query: str) -> TableManager[Any]:
        query = query.lower()

        expressions: list[Any] = []
        for column, dtype in self.nw_schema.items():
            if column == INDEX_COLUMN_NAME:
                continue
            if dtype == nw.String:
                expressions.append(nw.col(column).str.contains(f"(?i){query}"))
            elif dtype == nw.List(nw.String):
                # TODO: Narwhals doesn't support list.contains
                # expressions.append(
                #     nw.col(column).list.contains(query)
                # )
                pass
            elif (
                dtype.is_numeric()
                or is_narwhals_temporal_type(dtype)
                or dtype == nw.Boolean
            ):
                expressions.append(
                    nw.col(column).cast(nw.String).str.contains(f"(?i){query}")
                )

        if not expressions:
            return NarwhalsTableManager(self.data.filter(nw.lit(False)))

        or_expr = expressions[0]
        for expr in expressions[1:]:
            or_expr = or_expr | expr

        filtered = self.data.filter(or_expr)
        return NarwhalsTableManager(filtered)

    def get_summary(self, column: str) -> ColumnSummary:
        summary = self._get_summary_internal(column)
        for key, value in summary.__dict__.items():
            if value is not None:
                summary.__dict__[key] = unwrap_py_scalar(value)
        return summary

    def _get_summary_internal(self, column: str) -> ColumnSummary:
        # If column is not in the dataframe, return an empty summary
        if column not in self.nw_schema:
            return ColumnSummary()
        data = self.data.select(column)
        if isinstance(data, nw.LazyFrame):
            data = data.collect()

        col = data[column]
        total = len(col)
        if is_narwhals_string_type(col.dtype):
            return ColumnSummary(
                total=total,
                nulls=col.null_count(),
                unique=col.n_unique(),
            )
        if col.dtype == nw.Boolean:
            return ColumnSummary(
                total=total,
                nulls=col.null_count(),
                true=cast(int, col.sum()),
                false=cast(int, total - col.sum()),
            )
        if (col.dtype == nw.Date) or is_narwhals_time_type(col.dtype):
            return ColumnSummary(
                total=total,
                nulls=col.null_count(),
                min=col.min(),
                max=col.max(),
                mean=col.mean(),
                # Quantile not supported on date and time types
                # median=col.quantile(0.5, interpolation="nearest"),
            )
        if col.dtype == nw.Duration and isinstance(col.dtype, nw.Duration):
            unit_map = {
                "ms": (col.dt.total_milliseconds, "ms"),
                "ns": (col.dt.total_nanoseconds, "ns"),
                "us": (col.dt.total_microseconds, "μs"),
                "s": (col.dt.total_seconds, "s"),
            }
            method, unit = unit_map[col.dtype.time_unit]
            res = method()
            return ColumnSummary(
                total=total,
                nulls=col.null_count(),
                min=str(res.min()) + unit,
                max=str(res.max()) + unit,
                mean=str(res.mean()) + unit,
            )
        if is_narwhals_temporal_type(col.dtype):
            return ColumnSummary(
                total=total,
                nulls=col.null_count(),
                min=col.min(),
                max=col.max(),
                mean=col.mean(),
                median=col.quantile(0.5, interpolation="nearest"),
                p5=col.quantile(0.05, interpolation="nearest"),
                p25=col.quantile(0.25, interpolation="nearest"),
                p75=col.quantile(0.75, interpolation="nearest"),
                p95=col.quantile(0.95, interpolation="nearest"),
            )
        if (
            col.dtype == nw.List
            or col.dtype == nw.Struct
            or col.dtype == nw.Object
            or col.dtype == nw.Array
        ):
            return ColumnSummary(
                total=total,
                nulls=col.null_count(),
            )
        if col.dtype == nw.Unknown:
            return ColumnSummary(
                total=total,
                nulls=col.null_count(),
            )
        return ColumnSummary(
            total=total,
            nulls=col.null_count(),
            unique=(
                col.n_unique() if is_narwhals_integer_type(col.dtype) else None
            ),
            min=col.min(),
            max=col.max(),
            mean=col.mean(),
            median=col.quantile(0.5, interpolation="nearest"),
            std=col.std(),
            p5=col.quantile(0.05, interpolation="nearest"),
            p25=col.quantile(0.25, interpolation="nearest"),
            p75=col.quantile(0.75, interpolation="nearest"),
            p95=col.quantile(0.95, interpolation="nearest"),
        )

    def get_num_rows(self, force: bool = True) -> Optional[int]:
        # If force is true, collect the data and get the number of rows
        if force:
            return self.as_frame().shape[0]

        # When lazy, we don't know the number of rows
        if isinstance(self.data, nw.LazyFrame):
            return None

        # Otherwise, we can get the number of rows from the shape
        try:
            return self.data.shape[0]
        except Exception:
            # narwhals will raise on metadata-only frames
            return None

    def get_num_columns(self) -> int:
        return len(self.get_column_names())

    def get_column_names(self) -> list[str]:
        column_names = self.nw_schema.names()
        if INDEX_COLUMN_NAME in column_names:
            column_names.remove(INDEX_COLUMN_NAME)
        return column_names

    def get_unique_column_values(self, column: str) -> list[str | int | float]:
        try:
            return self.data[column].unique().to_list()
        except BaseException:
            # Catch-all: some libraries like Polars have bugs and raise
            # BaseExceptions, which shouldn't crash the kernel
            # If an exception occurs, try converting to strings first
            return self.data[column].cast(nw.String).unique().to_list()

    def get_sample_values(self, column: str) -> list[str | int | float]:
        # Skip lazy frames
        if isinstance(self.data, nw.LazyFrame):
            return []

        # Sample 3 values from the column
        SAMPLE_SIZE = 3
        try:
            from enum import Enum

            def to_primitive(value: Any) -> str | int | float:
                if isinstance(value, list):
                    return str([to_primitive(v) for v in value])
                elif isinstance(value, dict):
                    return str({k: to_primitive(v) for k, v in value.items()})
                elif isinstance(value, Enum):
                    return value.name
                elif isinstance(value, (float, int)):
                    return value
                return str(value)

            if self.data[column].dtype == nw.Datetime:
                # Drop timezone info for datetime columns
                # It's ok to drop timezone since these are just sample values
                # and not used for any calculations
                values = (
                    self.data[column]
                    .dt.replace_time_zone(None)
                    .head(SAMPLE_SIZE)
                    .to_list()
                )
            else:
                values = self.data[column].head(SAMPLE_SIZE).to_list()
            # Serialize values to primitives
            return [to_primitive(v) for v in values]
        except BaseException:
            # Catch-all: some libraries like Polars have bugs and raise
            # BaseExceptions, which shouldn't crash the kernel
            # May be metadata-only frame
            return []

    def sort_values(
        self, by: ColumnName, descending: bool
    ) -> TableManager[Any]:
        if isinstance(self.data, nw.LazyFrame):
            return self.with_new_data(
                self.data.sort(by, descending=descending, nulls_last=True)
            )
        else:
            return self.with_new_data(
                self.data.sort(by, descending=descending, nulls_last=True)
            )

    def __repr__(self) -> str:
        rows = self.get_num_rows(force=False)
        columns = self.get_num_columns()
        df_type = str(nw.get_native_namespace(self.data).__name__)
        if rows is None:
            return f"{df_type}: {columns:,} columns"
        return f"{df_type}: {rows:,} rows x {columns:,} columns"

    def _sanitize_table_value(self, value: Any) -> Any:
        """
        Sanitize a value for display in a table cell.

        Most values are unchanged, but some values are for better
        display such as Images.
        """
        if value is None:
            return None

        # Handle Pillow images
        if DependencyManager.pillow.imported():
            from PIL import Image

            if isinstance(value, Image.Image):
                return io_to_data_url(value, "image/png")
        return value
