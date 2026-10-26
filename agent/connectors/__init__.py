"""Read-only connectors to the owner's own services (see ``contract.py``)."""

from .contract import Connector, ConnectorStatus
from .email import EmailConnector, EmailSettings
from .plugin import ConnectorPlugin, ConnectorRegistry

__all__ = [
    "Connector",
    "ConnectorPlugin",
    "ConnectorRegistry",
    "ConnectorStatus",
    "EmailConnector",
    "EmailSettings",
]
