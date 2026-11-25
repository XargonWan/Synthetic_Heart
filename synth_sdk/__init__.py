"""SynthSDK: what an extension imports to talk to the Core.

An extension runs in its own process and reaches the Core **only** through this
package (see ``docs/plugin_platform``). It never imports ``core``.

This package is standard library only. It is also loaded by the Core gateway for
the shared wire protocol (:mod:`synth_sdk.rpc`), so both ends parse messages with
the same code.
"""

from synth_sdk.rpc import PROTOCOL_VERSION, ErrorCode, RpcError

__version__ = "0.1.0"

__all__ = ["PROTOCOL_VERSION", "ErrorCode", "RpcError", "__version__"]
