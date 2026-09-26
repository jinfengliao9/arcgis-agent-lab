"""Report generation.

Answers "what do we have, and can it be trusted" in one artifact:

* a **frozen fingerprint** (scenario hash, bridge commit, tool-catalogue hash,
  interpreter and ArcGIS versions, agent identity),
* sequence metrics **with intervals**, not bare point estimates,
* the failure distribution **with the remedy attached to each class**,
* limitations written by the author, before anyone asks.

Rendering is separated from computation so the same recorded run can be
re-rendered under different metric definitions -- and so a report can be
regenerated from files alone, with no ArcGIS and no model.
"""

from .build import Fingerprint, ReportInputs, render_report, write_report

__all__ = ["Fingerprint", "ReportInputs", "render_report", "write_report"]
