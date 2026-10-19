"""Named error types for HRS.

Every error is either a RowProblem (this person needs a look by hand, the run
carries on) or a SystemProblem (CCIS, the browser or HRS itself is broken, so
the run stops). main.py routes on which family an error belongs to, not on
individual error types. Ported from Project-Rap-Sheet's exceptions.py.
"""


class HrsError(Exception):
    """Base class for every HRS-specific error."""


class RowProblem(HrsError):
    """Something is wrong with this person's search or PDF. Flag it and move on."""


class SystemProblem(HrsError):
    """CCIS, the browser or HRS itself is broken. Stop the run."""


class ConfigProblem(SystemProblem):
    """config.yaml is missing something or holds a value that is refused."""
