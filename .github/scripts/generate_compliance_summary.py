import os
from pathlib import Path


def generate_summary() -> None:
    # Paths to log files or reports
    compliance_report = Path("mil_compliance.md")

    summary = ["## 🚀 Hermes Compliance Summary\n"]

    if compliance_report.exists():
        content = compliance_report.read_text()
        # Extract the high-level summary table if it exists
        summary.append(content)
    else:
        summary.append("⚠️ **mil_compliance.md** not found. Run `task validate` to generate it.")

    # Check for specific artifacts
    artifacts = {
        "Gerbers": "gerbers/HermesFC.gbr",  # Example path
        "BOM": "bom/HermesFC_BOM.csv",
        "Schematic PDF": "docs/Sensors.pdf",  # Example path
    }

    summary.append("\n### 📦 Generated Artifacts\n")
    for name, path in artifacts.items():
        status = "✅" if Path(path).exists() else "❌"
        summary.append(f"- {status} {name}")

    # Output for GitHub Actions
    with open(os.environ.get("GITHUB_STEP_SUMMARY", "summary.md"), "w") as f:
        f.write("\n".join(summary))


if __name__ == "__main__":
    generate_summary()
