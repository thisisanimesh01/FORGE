"""FORGE Phase 12 M1: Evidence-Driven Defect Diagnosis Engine.

Provides ground-truth evidence-backed defect diagnosis, localization,
contradiction detection, and finding generation.
"""

from forge.schemas import FindingConfidence, FindingSeverity
from forge.diagnosis.schemas import (
    CandidateLocation,
    DefectCategory,
    DiagnosisHypothesis,
    DiagnosisInput,
    DiagnosisResult,
    DiagnosisStatus,
    LocationSource,
    StaticFindingReport,
    TestFailureReport,
)
from forge.diagnosis.localization import FailureLocalizer
from forge.diagnosis.hypothesis import HypothesisEvaluator
from forge.diagnosis.engine import EvidenceDrivenDiagnosisEngine

__all__ = [
    "CandidateLocation",
    "DefectCategory",
    "DiagnosisHypothesis",
    "DiagnosisInput",
    "DiagnosisResult",
    "DiagnosisStatus",
    "EvidenceDrivenDiagnosisEngine",
    "FailureLocalizer",
    "FindingConfidence",
    "FindingSeverity",
    "HypothesisEvaluator",
    "LocationSource",
    "StaticFindingReport",
    "TestFailureReport",
]
