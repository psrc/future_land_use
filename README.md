# Future Land Use
FLU creation pipeline.

## Installation
1. Install UV package manager 

    `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"` 

2. Create a new example project by copying project/summer_2026 or just make modifications to settings in projects/summer_2026/configs/settings.yaml

3. Update file paths and settings in configs/settings.yaml

4. Run the pipeline using -c "<configs_dir>" cmd line arg
    
    `uv run future_land_use\run.py -c projects\summer_2026\configs`
