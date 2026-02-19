"""
Utility helpers for InfraForge.
"""

import os
import re
from datetime import datetime


def ensure_output_dir(output_dir: str) -> None:
    """Create the output directory if it doesn't exist."""
    os.makedirs(output_dir, exist_ok=True)


def save_generated_file(content: str, output_dir: str) -> str:
    """
    Save generated content to a file, auto-detecting the format.
    Returns the file path.
    """
    ensure_output_dir(output_dir)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Try to detect the output type from content
    ext = _detect_extension(content)
    filename = f"infraforge_{timestamp}{ext}"
    filepath = os.path.join(output_dir, filename)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

    return filepath


def _detect_extension(content: str) -> str:
    """Detect file extension based on content patterns."""
    content_lower = content.lower().strip()

    # Check for code blocks and extract language
    code_block_match = re.search(r"```(\w+)", content)
    if code_block_match:
        lang = code_block_match.group(1).lower()
        ext_map = {
            "bicep": ".bicep",
            "terraform": ".tf",
            "hcl": ".tf",
            "yaml": ".yml",
            "yml": ".yml",
            "json": ".json",
            "bash": ".sh",
            "shell": ".sh",
            "powershell": ".ps1",
        }
        if lang in ext_map:
            return ext_map[lang]

    # Fallback heuristics
    if "resource " in content_lower and ("param " in content_lower or "var " in content_lower):
        return ".bicep"
    if "resource " in content_lower and "provider " in content_lower:
        return ".tf"
    if "on:" in content_lower and "jobs:" in content_lower:
        return ".yml"
    if "trigger:" in content_lower and "stages:" in content_lower:
        return ".yml"

    return ".md"


def extract_code_blocks(content: str) -> list[dict]:
    """Extract all code blocks from markdown-formatted content."""
    blocks = []
    pattern = r"```(\w*)\n(.*?)```"
    for match in re.finditer(pattern, content, re.DOTALL):
        blocks.append({
            "language": match.group(1) or "text",
            "code": match.group(2).strip(),
        })
    return blocks
