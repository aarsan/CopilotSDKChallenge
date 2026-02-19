"""
Save output tool.
Saves generated IaC or pipeline content to files in the output directory.
"""

import os
from datetime import datetime

from pydantic import BaseModel, Field
from copilot import define_tool

from src.config import OUTPUT_DIR


class SaveOutputParams(BaseModel):
    content: str = Field(
        description="The content to save to a file (Bicep, Terraform, YAML, etc.)"
    )
    filename: str = Field(
        description=(
            "The filename to save as. Include the extension. "
            "Examples: 'main.bicep', 'main.tf', 'ci-cd.yml', 'azure-pipelines.yml'"
        )
    )
    subfolder: str = Field(
        default="",
        description=(
            "Optional subfolder within the output directory. "
            "Example: 'bicep', 'terraform', 'pipelines'"
        ),
    )


@define_tool(description=(
    "Save generated infrastructure code or pipeline configuration to a file. "
    "Use this tool after generating Bicep, Terraform, or pipeline YAML to persist "
    "the output to disk. Files are saved to the configured output directory."
))
async def save_output_to_file(params: SaveOutputParams) -> str:
    """Save content to the output directory."""
    target_dir = os.path.join(OUTPUT_DIR, params.subfolder) if params.subfolder else OUTPUT_DIR
    os.makedirs(target_dir, exist_ok=True)

    filepath = os.path.join(target_dir, params.filename)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(params.content)

    abs_path = os.path.abspath(filepath)
    return f"✅ Saved to: `{abs_path}`\n\nFile size: {len(params.content)} characters"
