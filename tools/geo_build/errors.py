from __future__ import annotations


class GeoBuildError(Exception):
    pass


class RoutingBackendError(GeoBuildError):
    pass


class GateFailure(GeoBuildError):
    pass


__all__ = ["GateFailure", "GeoBuildError", "RoutingBackendError"]
