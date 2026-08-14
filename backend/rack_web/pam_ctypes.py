# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# Vendored from python-pam 1.8.4 (Chris AtLee, David Ford; MIT).
# Stdlib + ctypes only; authenticates via system libpam (same as console login).

from __future__ import annotations

from ctypes import (
    CDLL,
    CFUNCTYPE,
    POINTER,
    Structure,
    byref,
    c_char,
    c_char_p,
    c_int,
    c_size_t,
    c_void_p,
    cast,
    memmove,
    sizeof,
)
from ctypes.util import find_library

__all__ = ("authenticate",)


class PamHandle(Structure):
    _fields_ = [("handle", c_void_p)]

    def __init__(self) -> None:
        super().__init__()
        self.handle = 0


class PamMessage(Structure):
    _fields_ = [("msg_style", c_int), ("msg", c_char_p)]


class PamResponse(Structure):
    _fields_ = [("resp", c_char_p), ("resp_retcode", c_int)]


conv_func = CFUNCTYPE(
    c_int,
    c_int,
    POINTER(POINTER(PamMessage)),
    POINTER(POINTER(PamResponse)),
    c_void_p,
)


class PamConv(Structure):
    _fields_ = [("conv", conv_func), ("appdata_ptr", c_void_p)]


PAM_PROMPT_ECHO_OFF = 1
PAM_PROMPT_ECHO_ON = 2
PAM_ERROR_MSG = 3
PAM_TEXT_INFO = 4
PAM_REINITIALIZE_CRED = 8

_libc_path = find_library("c")
_libpam_path = find_library("pam")
if not _libc_path or not _libpam_path:
    raise OSError("could not locate libc or libpam via find_library()")

libc = CDLL(_libc_path)
libpam = CDLL(_libpam_path)

calloc = libc.calloc
calloc.restype = c_void_p
calloc.argtypes = [c_size_t, c_size_t]

if hasattr(libpam, "pam_end"):
    pam_end = libpam.pam_end
    pam_end.restype = c_int
    pam_end.argtypes = [PamHandle, c_int]

pam_start = libpam.pam_start
pam_start.restype = c_int
pam_start.argtypes = [c_char_p, c_char_p, POINTER(PamConv), POINTER(PamHandle)]

pam_setcred = libpam.pam_setcred
pam_setcred.restype = c_int
pam_setcred.argtypes = [PamHandle, c_int]

pam_strerror = libpam.pam_strerror
pam_strerror.restype = c_char_p
pam_strerror.argtypes = [PamHandle, c_int]

pam_authenticate = libpam.pam_authenticate
pam_authenticate.restype = c_int
pam_authenticate.argtypes = [PamHandle, c_int]


class PamAuthenticator:
    code = 0
    reason: str | bytes | None = None

    def authenticate(
        self,
        username: str | bytes,
        password: str | bytes,
        service: str | bytes = "login",
        encoding: str = "utf-8",
        resetcreds: bool = True,
    ) -> bool:
        if isinstance(username, str):
            username = username.encode(encoding)
        if isinstance(password, str):
            password = password.encode(encoding)
        if isinstance(service, str):
            service = service.encode(encoding)

        if b"\x00" in username or b"\x00" in password or b"\x00" in service:
            self.code = 4
            self.reason = "strings may not contain NUL"
            return False

        cpassword = c_char_p(password)

        @conv_func
        def my_conv(n_messages, messages, p_response, app_data):
            addr = calloc(n_messages, sizeof(PamResponse))
            response = cast(addr, POINTER(PamResponse))
            p_response[0] = response
            for i in range(n_messages):
                if messages[i].contents.msg_style == PAM_PROMPT_ECHO_OFF:
                    dst = calloc(len(password) + 1, sizeof(c_char))
                    memmove(dst, cpassword, len(password))
                    response[i].resp = dst
                    response[i].resp_retcode = 0
            return 0

        handle = PamHandle()
        conv = PamConv(my_conv, 0)
        retval = pam_start(service, username, byref(conv), byref(handle))

        if retval != 0:
            self.code = retval
            self.reason = "pam_start() failed"
            return False

        retval = pam_authenticate(handle, 0)
        auth_success = retval == 0

        if auth_success and resetcreds:
            retval = pam_setcred(handle, PAM_REINITIALIZE_CRED)

        self.code = retval
        self.reason = pam_strerror(handle, retval)
        if isinstance(self.reason, bytes):
            self.reason = self.reason.decode(encoding, errors="replace")

        if hasattr(libpam, "pam_end"):
            pam_end(handle, retval)

        return auth_success


_pam_singleton = PamAuthenticator()


def authenticate(
    username: str | bytes,
    password: str | bytes,
    service: str | bytes = "login",
    encoding: str = "utf-8",
    resetcreds: bool = True,
) -> bool:
    return _pam_singleton.authenticate(username, password, service, encoding, resetcreds)


if __name__ == "__main__":  # pragma: no cover
    import getpass

    u = getpass.getuser()
    p = getpass.getpass()
    ok = authenticate(u, p)
    print(ok, _pam_singleton.code, _pam_singleton.reason)
