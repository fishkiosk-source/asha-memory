"""src.core — core.db graph store.

Split of v2 asha_memory_v2.py (~2600 lines). Each module <400 lines target,
no circular imports. store.py owns ALL connections (connect() invariant).
lexicon.py / clock.py carried over verbatim from v2 (Phase 1).
"""
