"""Windows user-bound secret storage for the local GUI.

The workspace config remains safe to copy and inspect. API keys are encrypted with Windows DPAPI
before they are written beside the workspace and can only be decrypted by the same Windows user on
the same computer.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import secrets
from ctypes import wintypes
from pathlib import Path

from smart_photo_triage.workspace import Workspace

_SECRET_FILENAME = ".spt-gui-secrets.json"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class GuiSecretError(ValueError):
    """A GUI credential could not be safely saved or loaded."""


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _secret_path(workspace: Workspace) -> Path:
    return workspace.root / _SECRET_FILENAME


def _windows_crypto() -> tuple[object, object]:
    if os.name != "nt":
        raise GuiSecretError("saving API keys in the GUI is currently supported on Windows only")
    crypt32 = ctypes.WinDLL("Crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("Kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return crypt32, kernel32


def _blob(value: bytes) -> tuple[_DataBlob, object]:
    buffer = ctypes.create_string_buffer(value)
    return _DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _protect(value: str) -> str:
    crypt32, kernel32 = _windows_crypto()
    source, _source_buffer = _blob(value.encode("utf-8"))
    encrypted = _DataBlob()
    if not crypt32.CryptProtectData(
        ctypes.byref(source),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(encrypted),
    ):
        raise GuiSecretError(
            f"Windows could not save this API key: {ctypes.WinError(ctypes.get_last_error())}"
        )
    try:
        encrypted_bytes = ctypes.string_at(encrypted.pbData, encrypted.cbData)
        return base64.b64encode(encrypted_bytes).decode("ascii")
    finally:
        kernel32.LocalFree(encrypted.pbData)


def _unprotect(value: str) -> str:
    try:
        encrypted_bytes = base64.b64decode(value.encode("ascii"), validate=True)
    except ValueError as error:
        raise GuiSecretError("the saved API key is invalid") from error
    crypt32, kernel32 = _windows_crypto()
    encrypted, _encrypted_buffer = _blob(encrypted_bytes)
    decrypted = _DataBlob()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(encrypted),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(decrypted),
    ):
        raise GuiSecretError(
            "the saved API key belongs to another Windows user or computer"
        ) from ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(decrypted.pbData, decrypted.cbData).decode("utf-8")
    except UnicodeDecodeError as error:
        raise GuiSecretError("the saved API key is invalid") from error
    finally:
        kernel32.LocalFree(decrypted.pbData)


def _load_tokens(workspace: Workspace) -> dict[str, str]:
    path = _secret_path(workspace)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        entries = payload["entries"]
    except (KeyError, OSError, TypeError, json.JSONDecodeError) as error:
        raise GuiSecretError("the saved GUI credential store is unreadable") from error
    if not isinstance(entries, dict) or any(
        not isinstance(provider_id, str) or not isinstance(token, str)
        for provider_id, token in entries.items()
    ):
        raise GuiSecretError("the saved GUI credential store is invalid")
    return dict(entries)


def _save_tokens(workspace: Workspace, entries: dict[str, str]) -> None:
    path = _secret_path(workspace)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        temporary.write_text(
            json.dumps({"version": 1, "entries": entries}, separators=(",", ":")), encoding="utf-8"
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def save_provider_secret(workspace: Workspace, provider_id: str, api_key: str) -> None:
    if not api_key:
        raise GuiSecretError("API key must not be empty")
    entries = _load_tokens(workspace)
    entries[provider_id] = _protect(api_key)
    _save_tokens(workspace, entries)


def load_provider_secret(workspace: Workspace, provider_id: str) -> str | None:
    token = _load_tokens(workspace).get(provider_id)
    return _unprotect(token) if token else None


def remove_provider_secret(workspace: Workspace, provider_id: str) -> bool:
    entries = _load_tokens(workspace)
    if provider_id not in entries:
        return False
    del entries[provider_id]
    _save_tokens(workspace, entries)
    return True
