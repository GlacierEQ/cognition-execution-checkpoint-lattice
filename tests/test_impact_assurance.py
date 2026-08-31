from __future__ import annotations
import unittest
from src.impact_assurance import ImpactVector, assess, rank

class ImpactAssuranceTests(unittest.TestCase):
    def test_high_value_contained_change_compounds(self):
        a = assess(ImpactVector(9, 9, 4, 9, 9, 9, 9))
        self.assertEqual(a.band, "COMPOUND")
        self.assertLess(a.risk, 4)
    def test_blast_radius_changes_priority(self):
        low = assess(ImpactVector(8, 8, 2, 5, 5, 8, 8))
        high = assess(ImpactVector(8, 8, 10, 5, 5, 8, 8))
        self.assertGreater(low.score, high.score)
    def test_invalid_vector_refuses(self):
        with self.assertRaises(ValueError): ImpactVector(11, 1, 1, 1, 1, 1, 1)
    def test_rank_is_descending(self):
        rows = rank([ImpactVector(5,5,5,5,5,5,5), ImpactVector(9,9,2,9,9,9,9)])
        self.assertGreaterEqual(rows[0].score, rows[1].score)

if __name__ == "__main__": unittest.main()
