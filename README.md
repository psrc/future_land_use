# Future Land Use
FLU creation pipeline.

## Installation
1. Install UV package manager 

    `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"` 

2. Install required R packages

    Open R or RStudio and run:
    ```r
    install.packages(c("stringr", "purrr", "magrittr", "data.table", "foreign", "readxl"))
    ```

3. Create a new example project by copying project/summer_2026 or just make modifications to settings in projects/summer_2026/configs/settings.yaml

4. Update file paths and settings in configs/settings.yaml

5. Run the pipeline using -c "<configs_dir>" cmd line arg
    
    `uv run future_land_use\run.py -c projects\summer_2026\configs`
