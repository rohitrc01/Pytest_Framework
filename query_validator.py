import os
import json
import logging
import pandas as pd
from utilities import logger

# OpenSpecimen query export CSVs typically contain 3 rows of metadata before the header.
SKIP_ROWS = 3

def validate_export(df_ref: pd.DataFrame, df_export: pd.DataFrame) -> tuple[str, str]:
    """Compares the exported DataFrame to the reference DataFrame."""
    try: 
        
        # Compare schemas (column names)
        if set(df_export.columns) != set(df_ref.columns):
            missing = set(df_ref.columns) - set(df_export.columns)
            extra = set(df_export.columns) - set(df_ref.columns)
            msg = []
            if missing: msg.append(f"Missing cols: {missing}")
            if extra: msg.append(f"Extra cols: {extra}")
            return "FAIL", " | ".join(msg)
            
        # Sort both dataframes to ignore row order completely
        df_export_sorted = df_export.sort_values(by=list(df_export.columns)).reset_index(drop=True)
        df_ref_sorted = df_ref.sort_values(by=list(df_ref.columns)).reset_index(drop=True)
        
        # Check row count
        if len(df_export_sorted) != len(df_ref_sorted):
            return "FAIL", f"Row count mismatch: Expected {len(df_ref_sorted)}, but got {len(df_export_sorted)}"
            
        # Compare data row-by-row (fill NaNs, convert to string, and strip invisible whitespaces)
        df_export_filled = df_export_sorted.fillna("").astype(str).apply(lambda x: x.str.strip() if hasattr(x, 'str') else x)
        df_ref_filled = df_ref_sorted.fillna("").astype(str).apply(lambda x: x.str.strip() if hasattr(x, 'str') else x)
        
        try:
            pd.testing.assert_frame_equal(df_ref_filled, df_export_filled, check_dtype=False)
            return "PASS", ""
        except AssertionError as e:
            err_str = str(e).splitlines()[0] if str(e) else "Data mismatch"
            return "FAIL", f"Data mismatch: {err_str}"
            
    except Exception as e:
        return "ERROR", f"CSV comparison error: {e}"

def validate_query_response(tc_id: str, resp_data: dict, reference_csv_path: str) -> tuple[str, str]:
    """Validates the POST response JSON from OpenSpecimen query API against a reference CSV.
    
    The HTTP POST is performed by execute_tc (like all other modules).
    This function only handles the post-response validation step.
    """
    try:
        column_labels = resp_data.get("columnLabels", [])
        rows = resp_data.get("rows", [])
        
        # OpenSpecimen's CSV generator natively replaces "# " with "_" in column headers.
        # Since we are fetching the raw JSON, we replicate that normalization here.
        normalized_labels = [label.replace("# ", "_") for label in column_labels]
        
        # Build pandas DataFrame for the exported data from the JSON response
        df_export = pd.DataFrame(rows, columns=normalized_labels)
        
        # Load reference data from file
        try:
            df_ref = pd.read_csv(reference_csv_path, skiprows=SKIP_ROWS)
        except Exception as e:
            return "FAIL", f"CSV comparison error: Reference file empty or invalid - {e}"
            
        return validate_export(df_ref, df_export)
        
    except Exception as e:
        return "ERROR", f"Workflow Error: {e}"