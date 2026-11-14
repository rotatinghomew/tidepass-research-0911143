import csv
import hashlib
import json
import math
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path


class DataError(ValueError):
    pass


class Catalog:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.manifest = json.loads((self.directory / "schema.json").read_text())
        self.tables = {}
        fingerprint = hashlib.sha256()
        fingerprint.update((self.directory / "schema.json").read_bytes())
        for name, schema in sorted(self.manifest.items()):
            raw = (self.directory / (name + ".csv")).read_bytes()
            fingerprint.update(name.encode())
            fingerprint.update(raw)
            with (self.directory / (name + ".csv")).open(newline="", encoding="utf-8") as source:
                reader = csv.DictReader(source)
                if reader.fieldnames != list(schema["columns"]):
                    raise DataError("字段顺序不符：" + name)
                records = []
                identifiers = set()
                for line_number, record in enumerate(reader, 2):
                    converted = {}
                    for field, definition in schema["columns"].items():
                        try:
                            converted[field] = self.convert(record[field], definition)
                        except (ValueError, TypeError, InvalidOperation) as error:
                            raise DataError(f"{name} 第 {line_number} 行字段 {field} 无效") from error
                    identifier = converted[schema["key"]]
                    if identifier is None or identifier in identifiers:
                        raise DataError("主键缺失或重复：" + name)
                    identifiers.add(identifier)
                    records.append(converted)
                self.tables[name] = records
        for name, schema in self.manifest.items():
            for field, target in schema.get("references", {}).items():
                table, target_field = target.split(".")
                allowed = {record[target_field] for record in self.tables[table]}
                for record in self.tables[name]:
                    if record[field] is not None and record[field] not in allowed:
                        raise DataError(f"关联记录不存在：{name}.{field}")
        self.fingerprint = fingerprint.hexdigest()

    @staticmethod
    def convert(value, definition):
        nullable = definition.endswith("?")
        kind = definition.rstrip("?")
        if value is None:
            raise DataError("字段缺失")
        if value == "" and nullable:
            return None
        if value == "":
            raise DataError("字段不允许空值")
        if kind == "int":
            return int(value)
        if kind == "number":
            number = float(value)
            if not math.isfinite(number):
                raise DataError("数值不是有限值")
            return number
        if kind == "money":
            number = Decimal(value)
            if not number.is_finite():
                raise DataError("金额不是有限值")
            return format(number, "f")
        if kind == "date":
            return date.fromisoformat(value).isoformat()
        if kind == "time":
            timestamp = datetime.fromisoformat(value)
            if timestamp.tzinfo is None:
                raise DataError("时间缺少时区")
            return timestamp.isoformat()
        if kind == "bool":
            if value not in ("0", "1"):
                raise DataError("布尔值仅支持零或一")
            return value == "1"
        if kind == "json":
            return json.loads(value)
        if kind != "text":
            raise DataError("未知字段类型")
        return value

    def rows(self, name):
        if name not in self.tables:
            raise KeyError(name)
        return json.loads(json.dumps(self.tables[name], ensure_ascii=False))

    def describe(self):
        return [{"name": name, "rows": len(records), "columns": self.manifest[name]["columns"]}
                for name, records in sorted(self.tables.items())]
