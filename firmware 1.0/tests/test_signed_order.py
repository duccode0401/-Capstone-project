"""Exercise the production order parser and replay guard without ESP32 or relay."""
import ctypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from ota_protocol import order_text, sign


class SignedOrderTests(unittest.TestCase):
    NOW = 1_800_000_000
    KEY = "42" * 32

    @classmethod
    def setUpClass(cls):
        local = ROOT / "build-host/tcc/tcc.exe"
        compiler = os.environ.get("CC") or (str(local) if local.exists() else shutil.which("cc"))
        idf = os.environ.get("IDF_PATH")
        if not compiler or not idf:
            raise unittest.SkipTest("Native C compiler and ESP-IDF cJSON source required")
        cjson = Path(idf) / "components/json/cJSON"
        directory = tempfile.TemporaryDirectory(prefix="sc-order-test-")
        cls.addClassCleanup(directory.cleanup)
        build = Path(directory.name)
        (build / "sc_core.h").write_text("""
#include <stdbool.h>
#include <stdint.h>
#include "cJSON.h"
#define SC_MESSAGE_MAX 1536
extern struct test_config { char device[32]; } sc_config;
bool sc_ota_enabled(void);
bool sc_verify_hmac(const char *,const char *);
cJSON *sc_parse_flat_json(const char *,size_t);
""", encoding="ascii")
        core = (ROOT / "components/sc_core/sc_core.c").read_text(encoding="utf-8")
        parser = core[core.index("cJSON *sc_parse_flat_json("):core.index("static esp_err_t put_string(")]
        replay = core[core.index("esp_err_t sc_commit_order_sequence("):core.index("bool sc_verify_hmac(")]
        wrapper = build / "wrapper.c"
        wrapper.write_text("""
#include <string.h>
#include "sc_core.h"
#include "signed_order.h"
#ifdef _WIN32
#define EXPORT __declspec(dllexport)
#else
#define EXPORT
#endif
typedef int esp_err_t;
#define ESP_OK 0
#define ESP_ERR_INVALID_STATE 1
#define ESP_ERR_NVS_NOT_FOUND 2
static int store, fail_commit;
static uint32_t last, pending;
typedef int (*verify_t)(const char *,const char *);
static verify_t verifier;
struct test_config sc_config={"ESP32_001"};
bool sc_ota_enabled(void) { return true; }
bool sc_verify_hmac(const char *text,const char *hex) { return verifier(text,hex)!=0; }
static int nvs_get_u32(int h,const char *key,uint32_t *value) {
    if(strcmp(key,"order_seq")) return 3;
    *value=last;
    return last?ESP_OK:ESP_ERR_NVS_NOT_FOUND;
}
static int nvs_set_u32(int h,const char *key,uint32_t value) {
    if(strcmp(key,"order_seq")) return 3;
    pending=value;
    return ESP_OK;
}
static int nvs_commit(int h) {
    if(fail_commit) return 4;
    last=pending;
    return ESP_OK;
}
""" + parser + replay + """
EXPORT int parse(const char *text,long long now,verify_t verify,int commit) {
    verifier=verify;
    sc_signed_order_t order;
    return sc_parse_signed_order(text,(time_t)now,&order) &&
        (!commit || sc_commit_order_sequence(order.sequence)==ESP_OK);
}
EXPORT void reset(uint32_t seq,int fail) { last=seq; fail_commit=fail; }
""", encoding="ascii")
        output = build / ("order.dll" if os.name == "nt" else "order.so")
        command = [compiler, "-shared", "-I", str(build), "-I", str(cjson),
                   "-I", str(ROOT / "smartcharge"), str(wrapper),
                   str(ROOT / "smartcharge/signed_order.c"), str(cjson / "cJSON.c"), "-o", str(output)]
        if os.name != "nt":
            command[1:1] = ["-fPIC"]
            command += ["-lm"]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(result.stderr)
        cls.lib = ctypes.CDLL(str(output))
        if os.name == "nt":
            import _ctypes
            cls.addClassCleanup(_ctypes.FreeLibrary, cls.lib._handle)
        cls.verifier_type = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p)
        cls.lib.parse.argtypes = [ctypes.c_char_p, ctypes.c_longlong, cls.verifier_type, ctypes.c_int]
        cls.lib.parse.restype = ctypes.c_int
        cls.lib.reset.argtypes = [ctypes.c_uint32, ctypes.c_int]
        cls.lib.reset.restype = None

    def setUp(self):
        from hmac import compare_digest

        def verify(text, signature):
            return int(compare_digest(sign(text.decode(), self.KEY).encode(), signature))

        self.verifier = self.verifier_type(verify)
        self.lib.reset(0, 0)

    def order(self, **overrides):
        order = dict(device="ESP32_001", command="START_CHARGE", port=1,
                     order_id="session-123", amount_vnd=10000, expires_at=self.NOW + 60, seq=5)
        order.update(overrides)
        order["signature"] = sign(order_text(order), self.KEY)
        return order

    def check(self, order, *, now=None, commit=False):
        payload = order if isinstance(order, str) else json.dumps(order)
        return self.lib.parse(payload.encode(), self.NOW if now is None else now, self.verifier, commit)

    def test_valid_signature_and_durable_sequence(self):
        self.assertTrue(self.check(self.order(), commit=True))
        self.assertFalse(self.check(self.order(), commit=True))
        self.assertFalse(self.check(self.order(seq=4), commit=True))
        self.lib.reset(5, 0)  # Simulate boot with the persisted replay floor.
        self.assertFalse(self.check(self.order(), commit=True))
        self.assertTrue(self.check(self.order(seq=6), commit=True))

    def test_storage_failure_never_authorizes_relay(self):
        self.lib.reset(0, 1)
        self.assertFalse(self.check(self.order(), commit=True))

    def test_rejects_legacy_unsigned_and_mutated_fields(self):
        self.assertFalse(self.check("ESP32_001 1 10000"))
        order = self.order()
        for key, value in (("amount_vnd", 20000), ("order_id", "session-124"),
                           ("expires_at", self.NOW + 61), ("seq", 6)):
            self.assertFalse(self.check(dict(order, **{key: value})), key)
        del order["signature"]
        self.assertFalse(self.check(order))

    def test_rejects_wrong_target_port_and_time_even_when_signed(self):
        for change in (dict(device="ESP32_002"), dict(port=2), dict(expires_at=self.NOW),
                       dict(expires_at=self.NOW + 301), dict(amount_vnd=1.5), dict(seq=0)):
            self.assertFalse(self.check(self.order(**change)), change)
        self.assertFalse(self.check(self.order(), now=0))
        duplicate = json.dumps(self.order()).replace('"seq": 5', '"seq": 5, "seq": 6')
        self.assertFalse(self.check(duplicate))
