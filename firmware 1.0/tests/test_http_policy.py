"""Run production HTTP guard C code natively; no network, ESP32 or flash writes.

CC can point at a native C compiler (TinyCC/GCC/Clang). Cross Xtensa Clang is not
a native compiler. On Windows a local build-host/tcc/tcc.exe is also recognized.
"""
import ctypes
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class HttpPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        local = ROOT / "build-host/tcc/tcc.exe"
        cc = os.environ.get("CC") or (str(local) if local.exists() else shutil.which("cc"))
        if not cc:
            raise unittest.SkipTest("Native C compiler unavailable; ESP-IDF builds still compile HTTP guards")
        cls.directory = tempfile.TemporaryDirectory(prefix="sc-http-test-", dir=ROOT / "build-host" if (ROOT / "build-host").exists() else None)
        directory = Path(cls.directory.name)
        source = directory / "wrapper.c"
        # Export wrappers only; implementation under test is the exact production C file.
        source.write_text('#include "sc_http_policy.c"\n'
            '#ifdef _WIN32\n#define EXPORT __declspec(dllexport)\n#else\n#define EXPORT\n#endif\n'
            'EXPORT int check_headers(int s, long long n, int c, const char *t, const char *e, long long m, int i) '
            '{ return sc_http_check_headers(s,n,c,t,e,m,i); }\n'
            'EXPORT int check_prefix(const unsigned char *b, size_t n, int i) { return sc_http_check_prefix(b,n,i); }\n', encoding="ascii")
        dll = directory / ("guard.dll" if os.name == "nt" else "guard.so")
        cmd = [cc, "-shared", "-I", str(ROOT / "components/sc_core"), "-I", str(ROOT / "components/sc_core/include"), str(source), "-o", str(dll)]
        if os.name != "nt":
            cmd.insert(1, "-fPIC")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode:
            cls.directory.cleanup()
            raise RuntimeError(result.stderr)
        cls.lib = ctypes.CDLL(str(dll))
        cls.lib.check_headers.argtypes = [ctypes.c_int, ctypes.c_longlong, ctypes.c_int,
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_longlong, ctypes.c_int]
        cls.lib.check_headers.restype = ctypes.c_int
        cls.lib.check_prefix.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_int]
        cls.lib.check_prefix.restype = ctypes.c_int

    @classmethod
    def tearDownClass(cls):
        if os.name == "nt":
            import _ctypes
            _ctypes.FreeLibrary(cls.lib._handle)
        del cls.lib
        cls.directory.cleanup()

    def headers(self, status=200, length=1024, chunked=0, media=b"application/octet-stream", encoding=b"identity", limit=2048, image=1):
        return self.lib.check_headers(status,length,chunked,media,encoding,limit,image)

    def test_django_manifest_and_binary_headers(self):
        self.assertEqual(self.headers(), 0)
        self.assertEqual(self.headers(media=b"application/json; charset=utf-8", image=0), 0)
        self.assertEqual(self.headers(media=b"APPLICATION/OCTET-STREAM"), 0)

    def test_redirect_auth_and_error_status_are_rejected(self):
        for status in (301, 302, 307, 308, 401, 403, 404, 429, 500, 503):
            self.assertNotEqual(self.headers(status=status), 0)

    def test_ngrok_html_even_with_status_200(self):
        self.assertEqual(self.headers(media=b"text/html; charset=utf-8"), 6)
        self.assertEqual(self.headers(media=b"application/xhtml+xml"), 6)

    def test_bad_length_chunked_compression_or_mime(self):
        for length in (-1, 0, 2049):
            self.assertNotEqual(self.headers(length=length), 0)
        self.assertNotEqual(self.headers(chunked=1), 0)
        self.assertNotEqual(self.headers(encoding=b"gzip"), 0)
        self.assertNotEqual(self.headers(media=b"application/json"), 0)
        self.assertNotEqual(self.headers(media=b""), 0)

    def test_html_disguised_as_binary_is_rejected_before_flash(self):
        for data in (b"<!DOCTYPE html><html>", b"  <HTML>ngrok warning", b"\n<head>warning"):
            self.assertEqual(self.lib.check_prefix(data,len(data),1), 6)
        self.assertEqual(self.lib.check_prefix(b"\xe9\x01\x00",3,1), 0)
        self.assertNotEqual(self.lib.check_prefix(b"not firmware",12,1), 0)

    def test_manifest_prefix_and_empty_inputs(self):
        self.assertEqual(self.lib.check_prefix(b' \n{"device":1}',14,0), 0)
        for data in (b"[]", b"", b"plain text"):
            self.assertNotEqual(self.lib.check_prefix(data,len(data),0), 0)


if __name__ == "__main__":
    unittest.main()
