"""Compatibility module for the account source's parcel normaliser."""
from .account import parcels as _account

globals().update(vars(_account))
