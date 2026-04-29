import sys
import unittest
from pathlib import Path

# Add scripts to path
sys.path.append(str(Path(__file__).parent.parent / "scripts"))

from check_pcb_integrity import PcbIntegrityChecker


class TestPcbIntegrity(unittest.TestCase):
    def setUp(self) -> None:
        self.test_pcb = Path("tests/fixtures/regression_sample.kicad_pcb")
        self.test_pcb.parent.mkdir(parents=True, exist_ok=True)

    def test_annular_ring_violation(self) -> None:
        # Create a mock PCB with a failing via (drill 0.3, size 0.5 -> annular 0.1 < 0.15)
        content = """
(kicad_pcb (version 20211014) (generator pcbnew)
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup (stackup (layer "F.Cu" (type "copper") (thickness 0.035)) (layer "B.Cu" (type "copper") (thickness 0.035))))
  (net 0 "")
  (net 1 "GND")
  (via (at 100 100) (size 0.5) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))
)
"""
        self.test_pcb.write_text(content)
        checker = PcbIntegrityChecker(str(self.test_pcb))
        checker.check_via_annular_ring()

        result = next(r for r in checker.results if r.name == "Annular Rings")
        assert not result.passed
        assert "1 vias have annular rings < 0.15mm" in result.detail

    def test_net_naming_violation(self) -> None:
        # Create a mock PCB with lowercase net name
        content = """
(kicad_pcb (version 20211014) (generator pcbnew)
  (net 0 "")
  (net 1 "GND")
  (net 2 "vcc_dirty")
)
"""
        self.test_pcb.write_text(content)
        checker = PcbIntegrityChecker(str(self.test_pcb))
        checker.check_net_names()

        result = next(r for r in checker.results if r.name == "Net Naming")
        assert not result.passed
        assert "contains lowercase characters" in result.detail

    def tearDown(self) -> None:
        if self.test_pcb.exists():
            self.test_pcb.unlink()


if __name__ == "__main__":
    unittest.main()
