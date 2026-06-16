import argparse
import re
import pandas as pd
from pathlib import Path

GDB_EXTENSION = '.gdb'
TABLE_EXTENSIONS = {'.csv', '.xlsx', '.xls'}


def clean_id(value):
    """Clean a unique ID: collapse whitespace to single underscore, remove non-alphanumeric except - and _."""
    s = str(value).strip() # convert to string and trim whitespace from ends
    s = re.sub(r'\s*-\s*', '-', s) # collapse spaces around hyphens
    s = re.sub(r'\s+', '_', s) # collapse all whitespace to single underscore
    s = re.sub(r'[^A-Za-z0-9_.-]', '', s) # remove all non-alphanumeric except dashes, underscores, and periods
    return s


def main():
    parser = argparse.ArgumentParser(description="Clean a unique ID field in a geodatabase layer or table.")
    parser.add_argument("file", help="Path to the input file (.gdb, .csv, .xlsx)")
    parser.add_argument("id_columns", nargs='+', help="Name(s) of the column(s) to clean")
    parser.add_argument("--layer", help="Layer name (required for .gdb files)")
    args = parser.parse_args()

    file_path = Path(args.file)
    ext = file_path.suffix.lower()

    if ext == GDB_EXTENSION:
        import geopandas as gpd
        if not args.layer:
            raise ValueError("--layer is required for geodatabase (.gdb) files")
        df = gpd.read_file(args.file, layer=args.layer)
        is_spatial = True
    elif ext in TABLE_EXTENSIONS:
        if ext in ('.xlsx', '.xls'):
            df = pd.read_excel(args.file)
        else:
            try:
                df = pd.read_csv(args.file)
            except UnicodeDecodeError:
                df = pd.read_csv(args.file, encoding='latin-1')
        is_spatial = False
    else:
        raise ValueError(f"Unsupported file extension '{ext}'. Supported: {{'{GDB_EXTENSION}'}} | {TABLE_EXTENSIONS}")

    crosswalk_cols = {}
    for col in args.id_columns:
        if col not in df.columns:
            raise ValueError(f"Column '{col}' not found. Available: {list(df.columns)}")
        original = df[col]
        cleaned = original.apply(clean_id)
        crosswalk_cols[f"{col}_original"] = original
        crosswalk_cols[f"{col}_cleaned"] = cleaned
        df[col] = cleaned

    crosswalk_df = pd.DataFrame(crosswalk_cols).drop_duplicates().reset_index(drop=True)
    crosswalk_path = str(file_path.with_suffix('')) + '_id_crosswalk.csv'
    crosswalk_df.to_csv(crosswalk_path, index=False)
    print(f"Crosswalk: {crosswalk_path} ({len(crosswalk_df)} rows)")

    if is_spatial:
        out_layer = args.layer + '_cleaned'
        df.to_file(args.file, driver='OpenFileGDB', layer=out_layer)
        print(f"Output: {args.file} / {out_layer} ({len(df)} rows)")
    else:
        out_path = str(file_path.with_suffix('')) + '_cleaned.csv'
        df.to_csv(out_path, index=False)
        print(f"Output: {out_path} ({len(df)} rows)")


if __name__ == '__main__':
    main()
