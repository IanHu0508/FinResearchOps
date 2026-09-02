# Core

Home of the FinAuditGate Module: `run()` / `replay()` in `engine.py`, the
bounded proposal snapshot codec in `proposal_snapshot.py`, the one public
synthetic profile and its alias registries in `synthetic_profile.py`, private
validation profiles in `profiles.py` and `private_profile.py`, offline trace
verification in `model_trace.py`, and canonical serialization plus append-once
writes in `artifacts.py`.

Case, Workpaper, human Review, and Change Packet export belong to the separate
Application Module, not here. Internal classes must not expand the external
Interface.
