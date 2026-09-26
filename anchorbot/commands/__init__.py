
from types import SimpleNamespace

from .admin import AdminCommands
from .router import Router
from .sticky import StickyCommands
from .utility import UtilityCommands


def configure(app):
    """Attach the complete command set used by both workers and startup sync."""
    app.router = Router(app)
    app.sticky = StickyCommands(app)
    app.utility = UtilityCommands(app)
    app.admin = AdminCommands(app)


def builders():
    app = SimpleNamespace()
    configure(app)
    return app.router.builders
