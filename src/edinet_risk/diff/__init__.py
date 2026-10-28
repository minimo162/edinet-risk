"""Diffs of matched item pairs, current vs previous period (plan §3 diff/, §6-6, §5 sentence_changes).

Two products: a character diff for people (chardiff: previous-only / current-only ranges) and a
sentence-level diff for the LLM (sentences: add/del/mod with evidence IDs and offsets). Minor changes
(minor: numbers / notation / whitespace only) are shown to people but kept out of the LLM input.
"""
