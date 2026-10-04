# Model implementation interface

`model.py` defines `CandidateModel.propose`, the internal interface for candidate generation. Scripted and local Ollama adapters implement it. A traced adapter also returns `ModelTraceReceipt`, connecting its proposal to saved request and response bytes.

Ports represent behavior with concrete alternative implementations; financial rules remain in the core and Case management remains in the Application.
