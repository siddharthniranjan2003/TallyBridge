import json
import os
import re
from functools import lru_cache

import xmltodict

from tally_client import TALLY_COMPANY, _fetch, _xml_escape
from xml_parser import clean_xml, get_messages, parse_tally_date, safe_float, safe_int, safe_str, _ensure_list


DEFINITIONS_FILE = os.path.join(
    os.path.dirname(__file__),
    "definitions",
    "structured_sections.json",
)


@lru_cache(maxsize=1)
def load_structured_definitions() -> dict:
    with open(DEFINITIONS_FILE, "r", encoding="utf-8") as handle:
        return json.load(handle)


def fetch_structured_section(section_name: str, request_context: dict | None = None):
    definition = _get_definition(section_name)
    xml_text = _fetch(build_collection_request(definition["request"], request_context=request_context))
    return parse_structured_section(section_name, xml_text)


def parse_structured_section(section_name: str, xml_text: str):
    definition = _get_definition(section_name)
    response_def = definition["response"]

    if response_def.get("mode") == "indexed_rows":
        return _parse_indexed_rows(section_name, xml_text, response_def)

    raw = xmltodict.parse(clean_xml(xml_text))
    rows = _extract_rows(raw, response_def)

    parsed_rows = []
    for row in rows:
        if not row:
            continue

        parsed = _parse_row(row, response_def)
        if parsed is None:
            continue

        parsed_rows.append(_finalize_section_row(section_name, parsed))

    if response_def.get("mode") == "single":
        return parsed_rows[0] if parsed_rows else {}

    return parsed_rows


def build_collection_request(request_def: dict, request_context: dict | None = None) -> str:
    request_type = request_def.get("type", "Collection")
    static_vars = [
        {"name": "SVEXPORTFORMAT", "value": "$$SysName:XML"},
        {"name": "SVCURRENTCOMPANY", "value": TALLY_COMPANY, "escape": True},
    ]
    static_vars.extend(request_def.get("static_variables", []))

    if request_type == "Data":
        return f"""
    <ENVELOPE>
      <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Data</TYPE>
        <ID>{_xml_escape(request_def["id"])}</ID>
      </HEADER>
      <BODY>
        <DESC>
          <STATICVARIABLES>
            {''.join(_render_static_variable(variable, request_context=request_context) for variable in static_vars)}
          </STATICVARIABLES>
        </DESC>
      </BODY>
    </ENVELOPE>"""

    fetch_xml = ", ".join(request_def.get("fetch", []))
    filters = request_def.get("filters", [])
    filters_xml = f"<FILTERS>{', '.join(filters)}</FILTERS>" if filters else ""
    formulas_xml = "".join(
        (
            f'<SYSTEM TYPE="FORMULAE" NAME="{_xml_escape(formula["name"])}">'
            f'{_xml_escape(formula["expression"])}'
            "</SYSTEM>"
        )
        for formula in request_def.get("system_formulae", [])
    )

    return f"""
    <ENVELOPE>
      <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>{_xml_escape(request_type)}</TYPE>
        <ID>{_xml_escape(request_def["id"])}</ID>
      </HEADER>
      <BODY>
        <DESC>
          <STATICVARIABLES>
            {''.join(_render_static_variable(variable, request_context=request_context) for variable in static_vars)}
          </STATICVARIABLES>
          <TDL>
            <TDLMESSAGE>
              <COLLECTION NAME="{_xml_escape(request_def['collection_name'])}" ISMODIFY="No">
                <TYPE>{_xml_escape(request_def["object_type"])}</TYPE>
                <FETCH>{_xml_escape(fetch_xml)}</FETCH>
                {filters_xml}
              </COLLECTION>
              {formulas_xml}
            </TDLMESSAGE>
          </TDL>
        </DESC>
      </BODY>
    </ENVELOPE>"""


def _get_definition(section_name: str) -> dict:
    definitions = load_structured_definitions()
    if section_name not in definitions:
        raise KeyError(f"Unknown structured section: {section_name}")
    return definitions[section_name]


def _render_static_variable(variable: dict, request_context: dict | None = None) -> str:
    type_attr = f' TYPE="{_xml_escape(variable["type"])}"' if variable.get("type") else ""
    value = _resolve_template(variable.get("value", ""), request_context or {})
    value_text = _xml_escape(str(value)) if variable.get("escape") else str(value)
    return f"<{variable['name']}{type_attr}>{value_text}</{variable['name']}>"


def _extract_rows(raw: dict, response_def: dict) -> list:
    row_path_options = response_def.get("row_path_options")
    if row_path_options:
        for row_path in row_path_options:
            rows = _extract_rows_from_path(raw, row_path)
            if rows:
                return rows
        return []

    collection = raw.get("ENVELOPE", {}).get("BODY", {}).get("DATA", {}).get("COLLECTION", {})
    return _ensure_sequence(collection.get(response_def["row_tag"], []))


def _extract_rows_from_path(raw: dict, row_path: str) -> list:
    if row_path.startswith("messages."):
        message_field = row_path.split(".", 1)[1]
        rows = []
        for message in get_messages(raw):
            if not message:
                continue
            rows.extend(_ensure_sequence(message.get(message_field)))
        return rows

    return _ensure_sequence(_resolve_source(raw, row_path))


def _parse_row(row: dict, response_def: dict):
    parsed = {}
    for field_name, field_def in response_def["fields"].items():
        parsed[field_name] = _transform_value(
            _first_non_empty(row, field_def.get("sources", [])),
            field_def,
        )

    for child_def in response_def.get("children", []):
        parsed[child_def["name"]] = _parse_children(row, child_def)

    required_field = response_def.get("required_field")
    if required_field and not parsed.get(required_field):
        return None

    return parsed


def _parse_children(row: dict, child_def: dict) -> list:
    child_rows = []
    for source_path in child_def.get("source_paths", []):
        child_rows = _ensure_list(_resolve_source(row, source_path))
        if child_rows:
            break

    parsed_children = []
    for child_row in child_rows:
        if not child_row:
            continue

        parsed_child = _parse_row(child_row, child_def)
        if parsed_child is None:
            continue

        parsed_children.append(parsed_child)

    return parsed_children


def _parse_indexed_rows(section_name: str, xml_text: str, response_def: dict):
    raw = xmltodict.parse(clean_xml(xml_text))
    root = raw.get("ENVELOPE", {})

    index_sources = {
        alias: _ensure_sequence(_resolve_source(raw, path) or _resolve_source(root, path))
        for alias, path in response_def.get("index_sources", {}).items()
    }

    length_alias = response_def.get("length_source")
    if length_alias and length_alias in index_sources:
        row_count = len(index_sources.get(length_alias, []))
    else:
        row_count = max((len(values) for values in index_sources.values()), default=0)

    parsed_rows = []
    for index in range(row_count):
        row_context = {
            alias: values[index] if index < len(values) else None
            for alias, values in index_sources.items()
        }

        parsed = {}
        for field_name, field_def in response_def["fields"].items():
            parsed[field_name] = _transform_value(
                _first_non_empty_indexed(row_context, field_def.get("sources", [])),
                field_def,
            )

        required_field = response_def.get("required_field")
        if required_field and not parsed.get(required_field):
            continue

        parsed_rows.append(_finalize_section_row(section_name, parsed))

    return parsed_rows


def _first_non_empty(row: dict, sources: list):
    for source in sources:
        value = _resolve_source(row, source)
        if not _is_empty(value):
            return value
    return None


def _first_non_empty_indexed(row_context: dict, sources: list):
    for source in sources:
        value = _resolve_indexed_source(row_context, source)
        if not _is_empty(value):
            return value
    return None


def _resolve_source(value, source: str):
    current = value
    parts = source.split(".")
    for index, part in enumerate(parts):
        if isinstance(current, list):
            current = current[0] if current else None
        if isinstance(current, dict):
            remaining_path = ".".join(parts[index:])
            if remaining_path in current:
                return current.get(remaining_path)
            current = current.get(part)
        else:
            return None
    return current


def _resolve_indexed_source(row_context: dict, source: str):
    if not source:
        return None

    alias, _, nested_path = source.partition(".")
    current = row_context.get(alias)
    if current is None:
        return None
    if not nested_path:
        return current
    return _resolve_source(current, nested_path)


def _transform_value(value, field_def: dict):
    transform = field_def.get("transform", "string")

    if transform == "string":
        result = _stringify(value)
    elif transform == "int":
        result = safe_int(value)
    elif transform == "float":
        result = safe_float(value)
    elif transform == "abs_float":
        result = abs(safe_float(value))
    elif transform == "date":
        result = parse_tally_date(_stringify(value))
    elif transform == "quantity_value_abs":
        result = abs(_quantity_value(value))
    elif transform == "quantity_unit":
        result = _quantity_unit(value)
    elif transform == "bool_yesno":
        result = _stringify(value).lower() == "yes"
    else:
        raise ValueError(f"Unsupported transform: {transform}")

    if _is_empty(result):
        return field_def.get("default", result)
    return result


def _stringify(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(part for part in (_stringify(item) for item in value) if part)
    return safe_str(value)


def _quantity_value(value) -> float:
    text = _stringify(value)
    if not text:
        return 0.0
    return safe_float(text.split()[0])


def _quantity_unit(value) -> str:
    text = _stringify(value)
    parts = text.split()
    if len(parts) > 1:
        return parts[1]
    if len(parts) == 1 and any(char.isalpha() for char in parts[0]):
        return parts[0]
    return ""


def _is_empty(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, list):
        return len(value) == 0
    if isinstance(value, dict):
        return len(value) == 0
    return False


def _ensure_sequence(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _resolve_template(value, request_context: dict) -> str:
    text = str(value)

    def replace(match):
        key = match.group(1)
        return str(request_context.get(key, ""))

    return re.sub(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}", replace, text)


def _finalize_section_row(section_name: str, row: dict) -> dict:
    if section_name == "vouchers":
        items = row.get("items") or []
        ledger_entries = row.get("ledger_entries") or []
        amount = safe_float(row.get("amount", 0))

        if amount == 0:
            if items:
                amount = sum(abs(safe_float(item.get("amount", 0))) for item in items)
            elif ledger_entries:
                party_amounts = [
                    abs(safe_float(entry.get("amount", 0)))
                    for entry in ledger_entries
                    if entry.get("is_party_ledger")
                ]
                if party_amounts:
                    amount = max(party_amounts)
                else:
                    amount = max(
                        (abs(safe_float(entry.get("amount", 0))) for entry in ledger_entries),
                        default=0,
                    )

        row["amount"] = abs(amount)
        row["items"] = items
        row["ledger_entries"] = ledger_entries
        return row

    if section_name == "profit_loss":
        raw_amount = safe_float(row.pop("_raw_amount", 0))
        return {
            "particulars": row.get("particulars", ""),
            "amount": abs(raw_amount),
            "is_debit": raw_amount >= 0,
        }

    if section_name == "balance_sheet":
        main_text = row.pop("_main_amount", "")
        sub_text = row.pop("_sub_amount", "")
        debit_text = row.pop("_debit_amount", "")
        credit_text = row.pop("_credit_amount", "")

        if safe_str(main_text) or safe_str(sub_text):
            raw_amount = safe_float(main_text) if safe_str(main_text) else safe_float(sub_text)
            side = "liability" if raw_amount > 0 else "asset"
            amount = abs(raw_amount)
        else:
            debit = abs(safe_float(debit_text))
            credit = abs(safe_float(credit_text))
            side = "asset" if debit else "liability"
            amount = debit if debit else credit

        return {
            "particulars": row.get("particulars", ""),
            "amount": amount,
            "side": side,
        }

    if section_name == "trial_balance":
        return {
            "particulars": row.get("particulars", ""),
            "debit": abs(safe_float(row.get("debit", 0))),
            "credit": abs(safe_float(row.get("credit", 0))),
        }

    return row
