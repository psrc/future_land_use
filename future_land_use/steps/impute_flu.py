import glob
import re
import os
import pandas as pd
import subprocess
from datetime import datetime
from future_land_use.util.pipeline import Pipeline


def _load_flu_imputed(p, flu_dir, juris_zn_id_col):
    """Find the most recent final_flu_imputed_*.csv in *flu_dir* by date suffix,
    rename ID column, drop cruft, and save to pipeline."""
    # Find all matching CSVs and extract the date from the filename
    pattern = os.path.join(flu_dir, 'final_flu_imputed_*.csv')
    candidates = glob.glob(pattern)
    if not candidates:
        raise FileNotFoundError(f"No final_flu_imputed_*.csv files found in {flu_dir}")

    dated = []
    for fpath in candidates:
        fname = os.path.basename(fpath)
        m = re.search(r'(\d{4}-\d{2}-\d{2})\.csv$', fname)
        if m:
            dated.append((datetime.strptime(m.group(1), '%Y-%m-%d'), fpath))

    if not dated:
        raise FileNotFoundError(
            f"No final_flu_imputed_YYYY-MM-DD.csv files (with a date) found in {flu_dir}"
        )

    path = max(dated, key=lambda x: x[0])[1]
    print(f"Reading in FLU imputed data (most recent: {os.path.basename(path)})...")
    f = (
        pd.read_csv(path)
        .rename(columns={juris_zn_id_col: 'juris_zn'})
    )
    # clean up f; remove extra/unnecessary fields before join
    drop_cols = [col for col in f.columns if col in ['Key', 'Zone', 'Definition'] or col.endswith('src')]
    f = f.drop(columns=drop_cols)
    p.save_table(f, 'flu_imputed')

# runs future_land_use/r_scripts/imputation_FLU2026.R as a subprocess
def run_step(context):
    print("Running step: impute_flu...")
    p = Pipeline(settings_path=context['configs_dir'])
    cfg = p.settings.get('imputation_settings', {})
    global_cfg = p.settings
    r_script_path = cfg.get('r_script_path', '')
    r_executable_path = cfg.get('r_executable_path', '')

    input_dir = global_cfg.get('root_dir', '')
    output_dir = cfg.get('output_dir', '')
    old_flu_crosswalk = cfg.get('old_flu_crosswalk', '')
    new_flu = global_cfg.get('flu_table_path', '')
    old_flu = cfg.get('old_flu', '')

    # Resolve paths relative to the project root if they are not absolute
    project_root = context.get('project_root', os.getcwd())

    def _resolve(path):
        return path if os.path.isabs(path) else os.path.join(project_root, path)

    r_script_path = _resolve(r_script_path)
    input_dir = _resolve(input_dir)
    output_dir = _resolve(output_dir) if os.path.isabs(output_dir) else os.path.join(input_dir, output_dir)
    old_flu_crosswalk = old_flu_crosswalk if os.path.isabs(old_flu_crosswalk) else os.path.join(input_dir, old_flu_crosswalk)
    new_flu = new_flu if os.path.isabs(new_flu) else os.path.join(input_dir, new_flu)
    old_flu = old_flu if os.path.isabs(old_flu) else os.path.join(input_dir, old_flu)

    # Build command-line arguments for the R script
    cmd = [
        r_executable_path,
        r_script_path,
        '--input-dir', input_dir,
        '--output-dir', output_dir,
        '--master-lookup', old_flu_crosswalk,
        '--new-flu', new_flu,
        '--old-flu', old_flu,
    ]

    # Run the R script from its own directory so that source("load_FLU2026.R") resolves
    r_script_dir = os.path.dirname(r_script_path)
    try:
        subprocess.run(cmd, check=True, cwd=r_script_dir)
        print("R script executed successfully.")
    except subprocess.CalledProcessError as e:
        print(f"Error occurred while running the R script: {e}")

    # imputed flu
    ROOT = global_cfg['root_dir']
    FLU_IMP_DIR = os.path.join(ROOT, cfg['flu_imputed_dir'])
    juris_zn_imputed_id = cfg['juris_zn_imputed_id'] # unique id column
    _load_flu_imputed(p, FLU_IMP_DIR, juris_zn_imputed_id)