"""Opt-in persistence backends for the domain ontology."""

from ncp.ontology.stores.sqlite import OntologyConflictError, SQLiteOntologyStore

__all__ = ["OntologyConflictError", "SQLiteOntologyStore"]
