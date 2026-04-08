import json
import os
import subprocess
import sys
from functools import lru_cache

from xml_parser import safe_float, safe_int, safe_str


DEFINITIONS_FILE = os.path.join(
    os.path.dirname(__file__),
    "definitions",
    "odbc_sections.json",
)


@lru_cache(maxsize=1)
def load_odbc_definitions() -> dict:
    with open(DEFINITIONS_FILE, "r", encoding="utf-8") as handle:
        return json.load(handle)


def get_tally_port() -> int:
    tally_url = os.environ.get("TALLY_URL", "http://localhost:9000").strip()
    if ":" not in tally_url:
        return 9000
    try:
        return int(tally_url.rsplit(":", 1)[1].rstrip("/"))
    except ValueError:
        return 9000


def _helper_path() -> str:
    return os.path.join(os.path.dirname(__file__), "tally_odbc_helper.ps1")


def _powershell_candidates() -> list[str]:
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    system32 = os.path.join(system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    wow64 = os.path.join(system_root, "SysWOW64", "WindowsPowerShell", "v1.0", "powershell.exe")
    candidates = []
    for path in (system32, wow64):
        if path and os.path.exists(path) and path not in candidates:
            candidates.append(path)
    return candidates


def _is_empty(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return False


def _transform_value(value, field_def: dict):
    transform = field_def.get("transform", "string")

    if transform == "string":
        result = safe_str(value)
    elif transform == "int":
        result = safe_int(value)
    elif transform == "float":
        result = safe_float(value)
    elif transform == "abs_float":
        result = abs(safe_float(value))
    elif transform == "quantity_value_abs":
        text = safe_str(value)
        result = abs(safe_float(text.split()[0] if text else 0))
    else:
        raise ValueError(f"Unsupported ODBC transform: {transform}")

    if _is_empty(result):
        return field_def.get("default", result)
    return result


def _row_lookup(row: dict, source: str):
    if source in row and not _is_empty(row[source]):
        return row[source]

    upper_key = source.upper()
    for key, value in row.items():
        if key.upper() == upper_key and not _is_empty(value):
            return value
    return None


def _normalize_rows(section_name: str, rows: list[dict]) -> list[dict]:
    definition = load_odbc_definitions()[section_name]
    required_field = definition.get("required_field")
    normalized_rows = []

    for raw_row in rows:
        if not isinstance(raw_row, dict):
            continue

        normalized = {}
        for field_name, field_def in definition["fields"].items():
            value = None
            for source in field_def.get("sources", []):
                value = _row_lookup(raw_row, source)
                if not _is_empty(value):
                    break
            normalized[field_name] = _transform_value(value, field_def)

        if required_field and _is_empty(normalized.get(required_field)):
            continue
        normalized_rows.append(normalized)

    return normalized_rows


class OdbcBridge:
    def __init__(self, dsn_override: str | None = None):
        self.dsn_override = (dsn_override or "").strip() or None
        self.port = get_tally_port()
        self._proc: subprocess.Popen | None = None
        self._probe_result: dict | None = None
        self._powershell_path: str | None = None

    def available(self) -> bool:
        return os.name == "nt" and os.path.exists(_helper_path()) and bool(_powershell_candidates())

    def close(self):
        if not self._proc:
            return
        try:
            self._send({"cmd": "quit"})
        except Exception:
            pass
        try:
            self._proc.kill()
        except Exception:
            pass
        for stream_name in ("stdin", "stdout", "stderr"):
            stream = getattr(self._proc, stream_name, None)
            if stream:
                try:
                    stream.close()
                except Exception:
                    pass
        self._proc = None

    def probe(self, sections: list[str]) -> dict:
        if self._probe_result is not None:
            return self._probe_result

        if not self.available():
            self._probe_result = {
                "state": "not_configured",
                "dsn": None,
                "supported_sections": [],
                "message": "ODBC helper is unavailable on this platform.",
            }
            return self._probe_result

        definitions = load_odbc_definitions()
        queries = {
            section: definitions[section]["query"]
            for section in sections
            if section in definitions
        }
        payload = {
            "cmd": "probe",
            "dsn_override": self.dsn_override,
            "port": self.port,
            "sections": list(queries.keys()),
            "queries": queries,
            "timeout_seconds": 8,
        }

        last_result = None
        for powershell_path in _powershell_candidates():
            self.close()
            self._powershell_path = powershell_path
            result = self._send(payload)
            if result.get("state") == "ok":
                self._probe_result = result
                return result
            last_result = result

        self._probe_result = last_result or {
            "state": "not_configured",
            "dsn": None,
            "supported_sections": [],
            "message": "No working Tally ODBC DSN was found.",
        }
        return self._probe_result

    def section_supported(self, section_name: str) -> bool:
        probe = self.probe([section_name])
        if probe.get("state") != "ok":
            return False
        return section_name in (probe.get("supported_sections") or [])

    def fetch_section(self, section_name: str) -> tuple[list[dict], dict]:
        definitions = load_odbc_definitions()
        if section_name not in definitions:
            return [], {
                "state": "unsupported",
                "message": f"No ODBC definition found for {section_name}.",
            }

        probe = self.probe([section_name])
        if probe.get("state") != "ok" or section_name not in (probe.get("supported_sections") or []):
            return [], probe

        result = self._send(
            {
                "cmd": "query",
                "dsn_override": self.dsn_override,
                "port": self.port,
                "sql": definitions[section_name]["query"],
                "timeout_seconds": 15,
            }
        )
        if result.get("state") not in {"ok", "empty"}:
            return [], result

        rows = result.get("rows") or []
        normalized_rows = _normalize_rows(section_name, rows)
        return normalized_rows, result

    def _start(self):
        if self._proc:
            return

        if not self._powershell_path:
            candidates = _powershell_candidates()
            if not candidates:
                raise RuntimeError("PowerShell is not available for the ODBC helper")
            self._powershell_path = candidates[0]

        self._proc = subprocess.Popen(
            [
                self._powershell_path,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                _helper_path(),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )

    def _send(self, payload: dict) -> dict:
        self._start()
        if not self._proc or not self._proc.stdin or not self._proc.stdout:
            raise RuntimeError("ODBC helper process is not available")

        if self._proc.poll() is not None:
            stderr_output = ""
            if self._proc.stderr:
                try:
                    stderr_output = self._proc.stderr.read()
                except Exception:
                    stderr_output = ""
            raise RuntimeError(f"ODBC helper exited unexpectedly. {stderr_output}".strip())

        self._proc.stdin.write(json.dumps(payload) + "\n")
        self._proc.stdin.flush()
        line = self._proc.stdout.readline()
        if not line:
            stderr_output = ""
            if self._proc.stderr:
                try:
                    stderr_output = self._proc.stderr.read()
                except Exception:
                    stderr_output = ""
            raise RuntimeError(f"ODBC helper did not return a response. {stderr_output}".strip())
        return json.loads(line)


def compare_section_rows(section_name: str, xml_rows: list[dict], odbc_rows: list[dict]) -> str | None:
    if len(xml_rows) != len(odbc_rows):
        return f"{section_name}: XML returned {len(xml_rows)} rows, ODBC returned {len(odbc_rows)} rows"

    if not xml_rows:
        return None

    sample_key = "name" if "name" in xml_rows[0] else next(iter(xml_rows[0].keys()), None)
    if not sample_key:
        return None

    xml_keys = sorted(str(row.get(sample_key, "")) for row in xml_rows)
    odbc_keys = sorted(str(row.get(sample_key, "")) for row in odbc_rows)
    if xml_keys != odbc_keys:
        return f"{section_name}: XML and ODBC returned different {sample_key} sets"

    return None
