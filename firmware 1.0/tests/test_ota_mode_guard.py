"""Run OTA mode/version gates from production C without writing flash."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class OtaGateTests(unittest.TestCase):
    def test_production_mode_and_version_gates(self):
        bundled = ROOT / "build-host/tcc/tcc.exe"
        cc = os.getenv("CC") or (str(bundled) if bundled.is_file() else shutil.which("cc"))
        if not cc:
            self.skipTest("Native C compiler required")
        core = (ROOT / "components/sc_core/sc_core.c").read_text(encoding="utf-8")
        gate = core[core.index("bool sc_ota_enabled("):core.index("esp_err_t sc_commit_order_sequence(")]
        version = core[core.index("static bool version_parts("):core.index("esp_err_t sc_init(")]
        code = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#define SC_PUBLIC_TEST_BROKER "mqtts://broker.emqx.io:8883"
static bool storage_ready=true;
static struct {
    bool mqtt_public_test_mode,mqtt_public_signed_mode;
    char key_hex[65],broker[192],mqtt_user[64],mqtt_password[128],trust_base[192];
} sc_config;
static bool sc_private_broker(const char *uri) { return !strcmp(uri,"mqtts://private.example:8883"); }
static bool sc_trusted_url(const char *url) { return !strcmp(url,"https://private.example/"); }
''' + gate + version + r'''
int main(void) {
    memset(sc_config.key_hex,'a',64);
    strcpy(sc_config.broker,"mqtts://private.example:8883");
    strcpy(sc_config.mqtt_user,"test");
    strcpy(sc_config.mqtt_password,"test");
    strcpy(sc_config.trust_base,"https://private.example/");
    assert(sc_ota_enabled());
    sc_config.mqtt_public_test_mode=true;
    assert(!sc_ota_enabled());
    sc_config.mqtt_public_test_mode=false;
    sc_config.mqtt_password[0]=0;
    assert(!sc_ota_enabled());
    strcpy(sc_config.mqtt_password,"test");
    sc_config.key_hex[0]='z';
    assert(!sc_ota_enabled());
    assert(sc_version_newer("1.1.0","1.0.0"));
    assert(!sc_version_newer("1.1.0","1.1.0"));
    assert(!sc_version_newer("1.0.9","1.1.0"));
    assert(!sc_version_newer("1.1.0-extra","1.0.0"));
    return 0;
}
'''
        with tempfile.TemporaryDirectory(prefix="sc-ota-gate-") as folder:
            source = Path(folder) / "gate.c"
            executable = Path(folder) / ("gate.exe" if os.name == "nt" else "gate")
            source.write_text(code, encoding="ascii")
            for cmd in ([cc, str(source), "-o", str(executable)], [str(executable)]):
                result = subprocess.run(cmd, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
