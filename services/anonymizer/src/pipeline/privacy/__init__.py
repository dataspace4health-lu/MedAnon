"""Risk-driven adaptive generalization package.

Provides the ARX-style closed-loop generalization capability:

  hierarchies   climbable generalization levels per QI attribute kind
  qi_index      out-of-core QI equivalence-class index (reuses analytics/risk)
  lattice       OLA-style lattice solver → GeneralizationPlan
  apply         apply plan to resources + Patient suppression

All modules are standalone; import only what you need.
"""
