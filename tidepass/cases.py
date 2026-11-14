import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


class CaseStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def create(self, catalog, label, options=None, notes=""):
        if not isinstance(label, str) or not label.strip() or len(label) > 120:
            raise ValueError("研究名称需为一至一百二十个字符")
        if options is None:
            options = {}
        if not isinstance(options, dict) or not isinstance(notes, str):
            raise ValueError("研究参数或备注格式无效")
        payload = {
            "id": uuid.uuid4().hex, "label": label.strip(), "options": options, "notes": notes,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_fingerprint": catalog.fingerprint,
            "tables": {name: catalog.rows(name) for name in catalog.tables},
        }
        envelope = {"content": payload, "sha256": hashlib.sha256(canonical(payload)).hexdigest()}
        with (self.directory / (payload["id"] + ".json")).open("xb") as target:
            target.write(canonical(envelope))
        return payload

    def read(self, identifier):
        if not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise KeyError(identifier)
        try:
            envelope = json.loads((self.directory / (identifier + ".json")).read_bytes())
        except FileNotFoundError as error:
            raise KeyError(identifier) from error
        content = envelope["content"]
        if hashlib.sha256(canonical(content)).hexdigest() != envelope["sha256"]:
            raise ValueError("研究记录完整性校验失败")
        return content

    def list(self):
        return [{field: record[field] for field in ("id", "label", "created_at", "source_fingerprint")}
                for record in (self.read(path.stem) for path in sorted(self.directory.glob("*.json")))]
