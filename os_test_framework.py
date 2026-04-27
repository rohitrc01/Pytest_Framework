import os, csv, json, pytest, requests, functools, logging, threading, io
from datetime import datetime
from typing import Optional
from utilities import logger, BASE_URL, get_token, CSVLogger, fetch_original_state, cleanup_or_revert_api_resource, \
    STATUS_FLD, VALID_STAT_FLD, ERR_FLD, HTTP_CODE_FLD, QUERY_TC_DATA_DIR
from query_validator import validate_query_response

# Load externalized messages
MSG_FILE = os.path.join(os.path.dirname(__file__), "messages.json")
with open(MSG_FILE, "r", encoding="utf-8") as f:
    MESSAGES = json.load(f)

def get_msg(msg_id, **kwargs):
    return MESSAGES.get(msg_id, msg_id).format(**kwargs)

# ── Config ────────────────────────────────────────────────────────────────────
GSHEET_CSV_URL      = os.getenv("GSHEET_CSV_URL")
INPUT_FILE          = os.getenv("INPUT_FILE", "input_data.csv")
SAVE_SNAPSHOT       = os.getenv("SAVE_INPUT_SNAPSHOT", "false").lower() == "true"
SNAPSHOT_DIR        = os.getenv("SNAPSHOT_DIR", "input_snapshots")
DATA_DIR            = os.getenv("DATA_DIR", "temp_test_data")


_env_output         = os.getenv("OUTPUT_FILE", "output_results.csv")
_base, _ext         = os.path.splitext(_env_output)
OUTPUT_FILE         = f"{_base}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{_ext}"

META_FIELDS  = json.loads(os.environ["META_FIELDS"])

# Overriding OUTPUT_EXTRA from .env to remove Latency_ms and Validation_Diff
OUTPUT_EXTRA = [STATUS_FLD, VALID_STAT_FLD, ERR_FLD, HTTP_CODE_FLD]

# ── Data Loading (Now Saves Locally First) ────────────────────────────────────

def load_test_cases(csv_url=GSHEET_CSV_URL, input_file=INPUT_FILE) -> list[dict]:
    """Downloads the GSheet to a local CSV file, then reads it."""
    
    # Ensure DATA_DIR exists
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)
        logger.info(f"📁 Created data directory: {DATA_DIR}")

    # If input_file is just a filename, prepend DATA_DIR
    if not os.path.dirname(input_file):
        input_file = os.path.join(DATA_DIR, input_file)

    if csv_url:
        logger.info(get_msg("INFO_DOWNLOADING_GSHEET"))
        try:
            resp = requests.get(csv_url, timeout=30)
            resp.raise_for_status()
            
            # 1. Save the primary input file locally
            with open(input_file, "w", encoding="utf-8") as f:
                f.write(resp.text)
            logger.info(get_msg("INFO_SAVED_LOCAL_TCS", input_file=input_file))

            # 2. Handle Snapshots (if enabled in .env)
            if SAVE_SNAPSHOT:
                if not os.path.exists(SNAPSHOT_DIR):
                    os.makedirs(SNAPSHOT_DIR)
                snap_path = os.path.join(SNAPSHOT_DIR, f"input_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
                with open(snap_path, "w", encoding="utf-8") as sf:
                    sf.write(resp.text)
                logger.info(get_msg("INFO_SNAPSHOT_ARCHIVED", snap_path=snap_path))

        except Exception as e:
            logger.warning(get_msg("WARN_DOWNLOAD_FAILED", e=e, input_file=input_file))

    # 3. Process the local file
    if not os.path.exists(input_file):
        logger.warning(get_msg("ERR_INPUT_FILE_NOT_FOUND", input_file=input_file) + " - Skipping this dataset.")
        return []

    with open(input_file, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    
    if not rows:
        logger.warning(get_msg("ERR_NO_TEST_CASES") + f" - Skipping file: {input_file}")
        return []
    return rows

# ── Payload Builder & Smart Casting ──────────────────────────────────────────

def _smart_cast(v: str):
    val = str(v).strip()
    if val.lower() == "true": return True
    if val.lower() == "false": return False
    if val.lower() in ("null", "none"): return None
    try:
        if "." in val: return float(val)
        return int(val)
    except ValueError:
        pass
    return val

def row_to_payload(row: dict) -> dict:
    payload = {}
    for k, v in row.items():
        k = k.strip()
        if not k or k in META_FIELDS or k in OUTPUT_EXTRA or k == "id" or v == "":
            continue

        if k == "distributingSites":
            if "distributingSites" not in payload:
                payload["distributingSites"] = {}
            # Support multiple groups separated by semicolon or pipe
            groups = str(v).split(";") if ";" in str(v) else str(v).split("|") if "|" in str(v) else [str(v)]
            for group in groups:
                parts = [x.strip() for x in group.split(",") if x.strip()]
                if parts:
                    inst = parts[0]
                    sites = parts[1:]
                    if inst not in payload["distributingSites"]:
                        payload["distributingSites"][inst] = [] # find a way to remove the hardcoding for the given resources
                    payload["distributingSites"][inst].extend(sites)
            continue

        value = _smart_cast(v)
        keys = k.split(".")
        current = payload

        for i, key in enumerate(keys[:-1]):
            next_key = keys[i + 1]
            if next_key.isdigit():
                if key not in current or not isinstance(current[key], list):
                    current[key] = []
                idx = int(next_key)
                
                is_last_key = (i + 1 == len(keys) - 1)
                
                while len(current[key]) <= idx:
                    current[key].append(None if is_last_key else {})
                
                if not is_last_key:
                    current = current[key][idx]
            elif not key.isdigit():
                if key not in current or not isinstance(current[key], dict):
                    current[key] = {}
                current = current[key]

        last_key = keys[-1]
        if last_key.isdigit():
            list_name = keys[-2]
            current[list_name][int(last_key)] = value
        else:
            current[last_key] = value

    if "activityStatus" not in payload:
        payload["activityStatus"] = "Active"
    return payload

# ── Deep Validation ───────────────────────────────────────────────────────────

def _norm(v) -> str:
    if isinstance(v, (int, float)):
        return str(float(v))
    if isinstance(v, str):
        try:
            return str(float(v))
        except ValueError:
            pass
    return str(v).strip().lower() if v is not None else ""

def compare_dicts(expected, actual, path="") -> list[str]:
    diffs = []
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{path}: expected dict, got {type(actual).__name__}"]
        for k, v in expected.items():
            diffs.extend(compare_dicts(v, actual.get(k), f"{path}.{k}" if path else k))
    elif isinstance(expected, list):
        if not isinstance(actual, list):
            return [f"{path}: expected list, got {type(actual).__name__}"]
        for i, ev in enumerate(expected):
            if isinstance(ev, dict):
                # For list-of-dicts: find any matching dict in actual
                found = any(not compare_dicts(ev, av) for av in actual if isinstance(av, dict))
            else:
                # For list-of-primitives (e.g. races, ethnicities strings): normalised match
                found = any(_norm(ev) == _norm(av) for av in actual)
            if not found:
                diffs.append(f"{path}[{i}]: no matching item found in response")
    else:
        actual_norm = _norm(actual)
        if _norm(expected) != actual_norm:
            diffs.append(f"{path}: expected='{expected}' | actual='{actual_norm}'")
    return diffs

def deep_validate(res_id: int, expected_payload: dict, headers: dict, api_url: str, actual_response: Optional[dict] = None, items_url_template: str = "") -> tuple[str, str]:
    try:
        # Optimization: If the POST response already contains fields to validate, use it
        if actual_response and any(k in actual_response for k in expected_payload if k != "id"):
            resp_data = actual_response
        else:
            validate_url = f"{api_url.rstrip('/')}/{res_id}"
            resp = requests.get(validate_url, headers=headers, timeout=15)
            if not resp.ok: return "Fail", f"GET HTTP {resp.status_code}"
            resp_data = resp.json()

        # Determine effective items URL template: use sheet-configured one, or auto-detect
        effective_template = items_url_template
        if not effective_template:
            # Auto-fallback: if expected_payload has any key ending in 'items' (e.g. orderItems),
            # try the standard /items sub-endpoint pattern
            items_key = next((k for k in expected_payload if k.lower().endswith("items")), None)
            if items_key:
                effective_template = f"{api_url.rstrip('/')}/{{id}}/items"
                logger.warning(f"🔍 [DIAG] No items_url_template from sheet — auto-detected fallback for key '{items_key}'")

        if effective_template:
            items_url = effective_template.replace("{orderId}", str(res_id)).replace("{id}", str(res_id))
            # Make absolute if the template is a relative path
            if items_url.startswith("/"):
                items_url = BASE_URL.rstrip("/") + items_url
            logger.warning(f"🔍 [DIAG] Fetching sub-list from: {items_url}")
            items_resp = requests.get(items_url, headers=headers, timeout=15)
            logger.warning(f"🔍 [DIAG] Sub-list fetch status: {items_resp.status_code}")
            if items_resp.ok:
                segments = [s for s in effective_template.rstrip("/").split("/") if s and "{" not in s]
                last_seg = segments[-1] if segments else "items"
                matched_key = next(
                    (k for k in expected_payload if k.lower().endswith(last_seg.lower())),
                    last_seg
                )
                resp_data[matched_key] = items_resp.json()
                # logger.warning(f"🔍 [DIAG] Injected {len(items_resp.json())} items into key '{matched_key}'")
                # logger.info(f"📦 Fetched sub-list '{matched_key}' from: {items_url}")
        else:
            # logger.warning(f"🔍 [DIAG] No items_url_template and no 'Items' key in payload — skipping sub-list fetch")
            pass

        diffs = compare_dicts(expected_payload, resp_data)
        return ("Pass", "") if not diffs else ("Fail", " | ".join(diffs))
    except Exception as exc:
        return "Error", str(exc)

# ── Execution ─────────────────────────────────────────────────────────────────

def execute_tc(row: dict) -> dict:
    api_url = row.pop("_api_url_", None)
    items_url_template = row.pop("_items_url_template_", "")
    if not api_url:
        err_msg = get_msg("ERR_MISSING_API_URL")
        logger.error(err_msg)
        return {**row, STATUS_FLD: "ERROR", ERR_FLD: err_msg}

    result = {**row}
    for field in OUTPUT_EXTRA:
        result[field] = ""
    result[STATUS_FLD] = "FAIL" # Set a default status
    try:
        role = row.get("Role", "admin").strip() # remove the default admin, should strictly abide as per roles defined in the sheet 
        token = get_token(role)
        headers = {"X-OS-API-TOKEN": token, "Content-Type": "application/json"}
        payload = row_to_payload(row)

        operation = row.get("Operation", "POST").strip().upper()
        res_id_input = str(row.get("id", "")).strip()

        if operation in ("PUT", "DELETE") and res_id_input:
            url = f"{api_url.rstrip('/')}/{res_id_input}"
        else:
            url = api_url



        original_state = None
        if operation == "PUT" and res_id_input:
            original_state = fetch_original_state(url, headers)

        # --- AUTO-FETCH PARTICIPANT ID (IF MISSING) ---
        if original_state and isinstance(original_state, dict):
            if "participant" in payload and isinstance(payload["participant"], dict):
                if "id" not in payload["participant"]:
                    orig_participant = original_state.get("participant")
                    if isinstance(orig_participant, dict) and "id" in orig_participant:
                        payload["participant"]["id"] = orig_participant["id"]
                        # logger.info(f"Auto-fetched participant.id = {orig_participant['id']} from state for Registration {res_id_input}")

        if operation == "POST":
            resp = requests.post(url, headers=headers, json=payload, timeout=15)
        elif operation == "PUT":
            resp = requests.put(url, headers=headers, json=payload, timeout=15)
        elif operation == "DELETE":
            resp = requests.delete(url, headers=headers, timeout=15)
        else:
            raise ValueError(f"Unsupported operation: {operation}")
        
        result[HTTP_CODE_FLD] = resp.status_code
        
        # Extract body but DO NOT save to result to keep CSV small
        try:
            resp_body = resp.json()
        except:
            resp_body = resp.text

        # Smart Validation: Empty Error Code = Positive Test, Provided Error Code = Negative Test
        expected_err = row.get("Expected_Error_Code", "").strip().lower()
        is_positive = not expected_err
        
        if is_positive:
            if resp.ok:
                result["TC_Status"] = "PASS"
                if operation != "DELETE":
                    ref_file = row.get("File_info", "").strip()

                    if ref_file:
                        # Query validation: compare POST response JSON against reference CSV
                        from pathlib import Path
                        found_paths = list(Path(QUERY_TC_DATA_DIR).rglob(ref_file))
                        if not found_paths:
                            raise ValueError(f"Reference file not found in {QUERY_TC_DATA_DIR}: {ref_file}")
                        v_stat, v_diff = validate_query_response(
                            row.get("TC_ID", "UNKNOWN"), resp_body, str(found_paths[0])
                        )
                        result[VALID_STAT_FLD] = v_stat
                        if v_stat != "PASS":
                            result[STATUS_FLD] = "FAIL"
                            result[ERR_FLD] = v_diff
                    else:
                        res_id = resp_body.get("id") if isinstance(resp_body, dict) else None
                        if not res_id and res_id_input:
                            res_id = res_id_input

                        if res_id:
                            # Optimized: Pass the POST response body to skip redundant GET if possible
                            v_stat, v_diff = deep_validate(res_id, payload, headers, api_url, actual_response=resp_body if isinstance(resp_body, dict) else None, items_url_template=items_url_template)
                            result[VALID_STAT_FLD] = v_stat
                            if v_stat != "Pass": 
                                result[STATUS_FLD] = "FAIL"
                                result[ERR_FLD] = v_diff
                            
                            # Teardown / Cleanup
                            cleanup_or_revert_api_resource(operation, api_url, headers, res_id, original_state)
            else:
                if isinstance(resp_body, list) and all(isinstance(e, dict) and "code" in e for e in resp_body):
                    codes = ", ".join([f"{e.get('code')} ({e.get('message', 'No message')})" for e in resp_body])
                    result[ERR_FLD] = f"Failed HTTP {resp.status_code} - errors: {codes}"
                elif isinstance(resp_body, dict) and "code" in resp_body:
                    result[ERR_FLD] = f"Failed HTTP {resp.status_code} - Code: {resp_body.get('code')}"
                else:
                    body_str = str(resp_body)[:400] if resp_body else ""
                    result[ERR_FLD] = f"Failed HTTP {resp.status_code}: {body_str}"
        else:
            
            # Extract all actual error codes from JSON response
            actual_codes = []
            if isinstance(resp_body, dict):
                actual_codes.append(str(resp_body.get("code", "")).lower())
            elif isinstance(resp_body, list):
                actual_codes.extend([str(err.get("code", "")).lower() for err in resp_body if isinstance(err, dict)])
            
            if not resp.ok:
                resp_str = json.dumps(resp_body) if isinstance(resp_body, dict) else str(resp_body)
                # PASS if (no code expected) OR (code in list) OR (code in body string)
                if not expected_err or any(expected_err == c for c in actual_codes) or expected_err in resp_str.lower():
                    result[STATUS_FLD] = "PASS"
                else:
                    result[ERR_FLD] = f"Code mismatch: expected='{expected_err}' | actual='{actual_codes}'"
            else:
                result[ERR_FLD] = "Expected fail but got HTTP 200"
    except Exception as e:
        result[ERR_FLD] = str(e)
    return result

# ── Integration & Export ──────────────────────────────────────────────────────

_os_logger = CSVLogger(OUTPUT_FILE, META_FIELDS + OUTPUT_EXTRA)

import hashlib

def run_tc(api_url: str, csv_url: str, metafunc, items_url_template: str = "") -> list[dict]:

    if "os_tc_row" in metafunc.fixturenames:
        input_filename = f"input_{hashlib.md5(csv_url.encode()).hexdigest()[:8]}.csv" if csv_url else INPUT_FILE
        test_cases = load_test_cases(csv_url, input_filename)
        for tc in test_cases:
            tc["_api_url_"] = api_url
            tc["_items_url_template_"] = items_url_template
        return test_cases
    return []


def execute_and_record_test(tc_row, record_property):
    res = execute_tc(tc_row)
    
    # Streaming Write to CSV
    _os_logger.write_row(res)

    for field in OUTPUT_EXTRA:
        record_property(field, str(res.get(field, "")))
    if res[STATUS_FLD] != "PASS":
        pytest.fail(f"[{tc_row.get('TC_ID')}] {res.get(ERR_FLD)}", pytrace=False)

def cleanup_temp_data():
    """Deletes all files within DATA_DIR and removes the directory."""
    if os.path.exists(DATA_DIR):
        import shutil
        shutil.rmtree(DATA_DIR)
        logger.info(f"🧹 Cleaned up temporary data directory: {DATA_DIR}") 