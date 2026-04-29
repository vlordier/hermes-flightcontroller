from pathlib import Path

from scripts.check_bom import _read_bom, check_esd_tvs


def test_bom_pydantic_validation(tmp_path: Path) -> None:
    """Test that BOM rows with missing LCSC or invalid quantity fail Pydantic validation."""
    bom_file = tmp_path / "invalid_bom.csv"
    # Row 1: missing lcsc → fails field_validator
    # Row 2: quantity=0 → fails Field(gt=0)
    content = (
        "Designator,Footprint,Quantity,Value,LCSC Part #\nC1,0603,1,100n,\nC2,0603,0,10u,C12345"
    )
    bom_file.write_text(content)

    rows = _read_bom(bom_file)
    assert len(rows) == 0  # Both should be skipped due to validation errors


def test_tvs_detection(tmp_path: Path) -> None:
    """Test TVS detection logic with configured patterns."""
    bom_file = tmp_path / "tvs_bom.csv"
    content = "Designator,Footprint,Quantity,Value,LCSC Part #\nD1,SOD-323,1,tvs_diode,C001\nU1,USB_ESD,1,USBLC6,C002"
    bom_file.write_text(content)

    rows = _read_bom(bom_file)
    result = check_esd_tvs(rows)
    assert result.passed is True
    assert "Found 2 device(s)" in result.detail
