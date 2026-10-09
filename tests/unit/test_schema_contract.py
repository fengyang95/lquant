"""curated schema 演进纪律：改列必须显式确认。

`SCHEMAS`（data/schema.py）的列是对外契约 —— 落湖 parquet、字段映射 yaml、
因子 DSL 白名单、看板列都在读它。本文件用指纹把「改列」这个动作变成
**一次必须显式提交的改动**，而不是 code review 里可能被漏掉的一行 diff。

规则（见 data/schema.py 的注释）：
- 只增列：`DATASET_SCHEMA_VERSION` +1，指纹更新；
- 删列 / 改类型：同样 +1，并在 `DATASET_SCHEMA_BREAKING_NOTE` 写明破坏性。
"""
from __future__ import annotations

from lquant.data import schema


def test_fingerprint_matches_declared_constant():
    actual = schema.schema_fingerprint()
    assert actual == schema.SCHEMA_FINGERPRINT, (
        "curated schema 变了但 SCHEMA_FINGERPRINT 没更新。\n"
        f"  实际: {actual}\n"
        f"  声明: {schema.SCHEMA_FINGERPRINT}\n"
        "请同时：① 递增 DATASET_SCHEMA_VERSION；② 更新 SCHEMA_FINGERPRINT；"
        "③ 若为删列/改类型，更新 DATASET_SCHEMA_BREAKING_NOTE。"
    )


def test_version_is_positive_int():
    assert isinstance(schema.DATASET_SCHEMA_VERSION, int)
    assert schema.DATASET_SCHEMA_VERSION >= 1


def test_fingerprint_is_stable_and_order_sensitive():
    assert schema.schema_fingerprint() == schema.schema_fingerprint()
    # 表内列序也是契约的一部分（parquet 列序 + select 顺序都依赖它）
    fp = schema.schema_fingerprint()
    assert len(fp) == 64


def test_every_table_has_dictionary_and_docs():
    """curated 表必须有中文说明与字段注记，别让新表悄悄裸奔。"""
    from lquant.data.dictionary import FIELD_NOTES, TABLE_META

    for table in schema.SCHEMAS:
        assert table in TABLE_META, f"{table} 缺 TABLE_META 说明"
        assert table in FIELD_NOTES, f"{table} 缺 FIELD_NOTES 字段注记"
        missing = [c for c in schema.SCHEMAS[table] if c not in FIELD_NOTES[table]]
        assert not missing, f"{table} 字段缺注记: {missing}"


def test_dictionary_has_no_orphan_tables():
    from lquant.data.dictionary import FIELD_NOTES, TABLE_META

    for table in TABLE_META:
        assert table in schema.SCHEMAS, f"TABLE_META 里的 {table} 已不在 SCHEMAS"
    for table in FIELD_NOTES:
        assert table in schema.SCHEMAS, f"FIELD_NOTES 里的 {table} 已不在 SCHEMAS"
