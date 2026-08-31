"""Deterministic impact assurance for Execution Checkpoint Lattice.

This module turns architectural impact into an executable decision surface.
It is intentionally dependency-free so CI, operators, and downstream tools can
score changes before they are promoted.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from math import isfinite
from typing import Iterable

REPOSITORY = "cognition-execution-checkpoint-lattice"
COMPANY_LENS = "Cognition"
INNOVATION = "Execution Checkpoint Lattice"

@dataclass(frozen=True, slots=True)
class ImpactVector:
    near_term_value: float
    long_term_leverage: float
    failure_blast_radius: float
    reversibility: float
    evidence_strength: float
    company_fit: float
    cross_repo_compounding: float

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if not isfinite(value) or not 0.0 <= value <= 10.0:
                raise ValueError(f"{name} must be finite and within [0, 10]")

@dataclass(frozen=True, slots=True)
class ImpactAssessment:
    score: float
    risk: float
    leverage: float
    band: str
    vector: ImpactVector

    def as_dict(self) -> dict[str, object]:
        return {"repository": REPOSITORY, "company_lens": COMPANY_LENS, "innovation": INNOVATION,
                "score": self.score, "risk": self.risk, "leverage": self.leverage,
                "band": self.band, "vector": asdict(self.vector)}

def assess(vector: ImpactVector) -> ImpactAssessment:
    leverage = (
        0.22 * vector.near_term_value + 0.22 * vector.long_term_leverage
        + 0.16 * vector.company_fit + 0.14 * vector.evidence_strength
        + 0.16 * vector.cross_repo_compounding + 0.10 * vector.reversibility
    )
    # High blast radius is tolerable only when reversibility/evidence are strong.
    containment = (vector.reversibility + vector.evidence_strength) / 20.0
    risk = vector.failure_blast_radius * (1.0 - 0.65 * containment)
    score = max(0.0, min(10.0, leverage - 0.18 * risk))
    band = "COMPOUND" if score >= 8.0 else "ADVANCE" if score >= 6.0 else "HARDEN" if score >= 4.0 else "REWORK"
    return ImpactAssessment(round(score, 3), round(risk, 3), round(leverage, 3), band, vector)

def rank(vectors: Iterable[ImpactVector]) -> list[ImpactAssessment]:
    return sorted((assess(v) for v in vectors), key=lambda a: (a.score, a.leverage), reverse=True)
