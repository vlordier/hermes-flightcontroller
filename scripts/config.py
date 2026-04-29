import re
from re import Pattern

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MilSettings(BaseSettings):  # type: ignore[misc]
    """Global configuration for MIL-SPEC/IPC-2221A thresholds."""

    # BOM Thresholds
    min_decoupling_caps: int = Field(default=5, description="Min number of decoupling capacitors")
    tvs_patterns: list[str] = Field(
        default=["tvs", "USBLC", "ESD", "PRTR", "PESD", "TPD"],
        description="Regex patterns for TVS/ESD diodes",
    )

    # PCB Thresholds
    min_track_width_mm: float = Field(default=0.25, description="MIL-STD-275E signal track width")
    min_via_drill_mm: float = Field(default=0.30, description="MIL-STD-275E PTH minimum")
    min_annular_ring_mm: float = Field(default=0.125, description="IPC-2221A Class C Space Minimum")

    # Power Net Names (for SPICE validation)
    expected_rails: dict[str, tuple[float, float]] = Field(
        default={
            r"v\(3v3\)": (3.13, 3.47),
            r"v\(1v1\)": (1.04, 1.16),
            r"v\(1v8\)": (1.71, 1.89),
            r"v\(vbus\)": (4.5, 5.5),
        }
    )

    model_config = SettingsConfigDict(env_prefix="HERMES_", env_file=".env", extra="ignore")

    @property
    def tvs_regex(self) -> list[Pattern[str]]:
        return [re.compile(p, re.IGNORECASE) for p in self.tvs_patterns]


settings = MilSettings()
