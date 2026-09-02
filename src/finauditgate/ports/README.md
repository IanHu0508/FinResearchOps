# Internal Seams

`model.py` defines the one internal Seam that genuinely varies: candidate
generation (`CandidateModel.propose`), with the scripted Adapter and the local
Ollama Adapter as its two implementations, plus the `ModelTraceReceipt` a
traced Adapter returns alongside its proposal.

Add another Seam here only when a second implementation actually exists.
