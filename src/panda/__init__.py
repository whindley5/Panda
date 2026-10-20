"""Offline PII sanitiser for client communication files.

Everything in this package must work with the network switched off. No call may
reach an external service, and no client data may be written anywhere except the
configured output and report directories.
"""

__version__ = "0.1.0"
