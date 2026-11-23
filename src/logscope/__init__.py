"""logscope -- stream logs into ranked incidents.

Public API::

    from logscope import analyze
    analysis = analyze("app.log")
    for incident in analysis.incidents:
        print(incident.title, incident.severity_label)
"""

from __future__ import annotations

from logscope.engine import Analysis, analyze, analyze_lines, parse_time_spec
from logscope.formats import Event
from logscope.miner import Cluster, Miner

__all__ = ["Analysis", "Event", "Cluster", "Miner", "analyze", "analyze_lines",
           "parse_time_spec", "__version__"]
__version__ = "1.0.0"
