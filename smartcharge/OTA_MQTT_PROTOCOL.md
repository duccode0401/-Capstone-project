# SmartCharge OTA MQTT protocol

Backend implementation follows the current ESP-IDF firmware in `firmware/components/sc_core`.
Firmware is not modified by this change.

## Topics

| Direction | Topic | QoS |
|---|---|---:|
| Backend → ESP32 | `iot/charge_station/v1/ota/command` | 1 |
| ESP32 → Backend | `iot/charge_station/v1/ota/status` | 1 |
| ESP32 → Backend | `iot/charge_station/v1/telemetry` | 0 |
| ESP32 → Backend | `iot/charge_station/v1/logs` | 0 |
| Backend → ESP32 | `iot/charge_station/v1/orders` | 1 |
| ESP32 → Backend | `iot/charge_station/v1/status` | 1 |

The last two topics and the existing DONE payload remain unchanged.

## Authenticated OTA command

```json
{
  "device": "ESP32_001",
  "command": "START_OTA",
  "request_id": "ota-0123456789abcdef0123456789abcdef",
  "version": "1.1.0",
  "manifest_url": "https://ota.example.com/firmware/manifest/REQUEST/TOKEN/",
  "seq": "42",
  "signature": "lowercase-hmac-sha256"
}
```

`seq` is a decimal JSON string because the current firmware parser requires a
string. It increases per device and is stored in ESP32 NVS before an OTA request
is accepted. The signature is HMAC-SHA256 with the device's 32-byte key over:

```text
SC-CMD-1
DEVICE
START_OTA
REQUEST_ID
VERSION
MANIFEST_URL
SEQ
```

MQTT must use TLS, a unique client identity and authenticated ACLs. ESP32 clients
may publish only their own telemetry/log/status topics and subscribe only to
orders/OTA commands intended for them. The Django backend may publish commands
and subscribe to device output. The shared public broker is acceptable for the
existing development flow only; the backend refuses OTA publication unless MQTT
username and password are configured.

## Manifest and firmware download

The command carries a random short-lived capability in the manifest URL path.
The manifest produces a similarly protected download URL. Proxies must disable
or redact access logs for `/firmware/` so capabilities are not written to logs.

Manifest fields are `device`, `request_id`, `version`, `url`, `size`, `sha256`,
`chip`, `seq`, and `signature`. Its signature canonical form is:

```text
SC-MANIFEST-1
DEVICE
REQUEST_ID
VERSION
DOWNLOAD_URL
SIZE
SHA256
esp32
SEQ
```

SHA-256 detects file corruption. HMAC authenticates the publisher and binds the
manifest to one device/job. SHA-256 by itself is not source authentication.
The firmware additionally verifies the ESP image header, chip ID, app descriptor,
project name/version and ESP-IDF image checksum before changing the boot slot.

## Status handling

Supported states: `PENDING`, `ACCEPTED`, `REJECTED_BUSY`, `DOWNLOADING`,
`VERIFYING`, `INSTALLING`, `REBOOTING`, `SUCCESS`, `FAILED`, `RECOVERING`.
`PENDING` is created by the server. Duplicate status/progress messages are
idempotent and progress may not move backwards. A status must match both device
and request ID. `SUCCESS` is accepted only with the firmware version attached to
the job; current firmware reports it only after the new image boots and passes
health checks. Stalled jobs are marked `FAILED` by the subscriber timeout worker.

MQTT PUBACK means the broker accepted a command. It does not mean the ESP32
accepted, installed, booted or validated the firmware.

## Telemetry and logs

Telemetry follows the firmware schema and arrives every 10 seconds. The server
accepts a bounded interval of 5–20 seconds, validates numeric ranges, stores UTC
timestamps, and never creates or modifies charging sessions from telemetry.

Device logs accept levels `INFO`, `WARN`/`WARNING`, and `ERROR`. `WARN` is stored
as `WARNING`. Messages that appear to contain credentials are replaced with a
redaction marker. Retention defaults to 10,000 telemetry rows and 5,000 log rows
per device and is configurable through Django settings.
