# OTA Admin usage

## Permissions

The operator must be staff and have these Django permissions:

- `Firmware artifact | Can add/view/change firmware artifact`
- `OTA job | Can view OTA job`
- `OTA job | Can deploy approved firmware`
- `Device telemetry | Can view device telemetry`
- `Device log | Can view device log`

Grant permissions through Django Admin → Users/Groups. Keep firmware approval and
deployment limited to trusted operators.

## Upload and approve firmware

Open Admin → Firmware artifacts → Add. Choose the application `.bin`, set the
same semantic version embedded by ESP-IDF, select target `esp32`, and select its
type. The server checks extension, size, ESP image magic, segment count, ESP32
chip ID, ESP-IDF app descriptor, embedded version and project name, then computes
SHA-256. Main firmware must be smaller than `0x2F0000` bytes.

Review the SHA-256/build provenance before checking **Approved**. Recovery images
may be catalogued, but the OTA form never offers Recovery and backend validation
also rejects it. Bootloader and partition-table binaries fail app-image/project
validation and cannot use this flow.

## Start and monitor OTA

1. Confirm the device has fresh `IDLE` telemetry and no active charging session.
2. Open Admin → OTA jobs → Add.
3. Select device and approved main firmware.
4. Check the explicit confirmation box and save.
5. Watch status/progress in the OTA job list.

Server validation rejects stale/offline/unknown devices, active charging,
unapproved or wrong-target images, missing HTTPS/HMAC settings, and unauthenticated
MQTT configuration. A `PENDING` job only means the command was published. Wait for
`ACCEPTED`, download/verify/install/reboot states, and finally `SUCCESS` after the
new firmware reports the expected version.

## Monitoring and troubleshooting

- `/dashboard/devices/`: latest power, energy, charging state, version and online state.
- `/dashboard/logs/`: filter by device and `ALL`, `INFO`, `WARNING`, `ERROR`, `OTA`, or `CHARGING`.
- Admin → Device telemetry / Device logs: detailed filtering and timestamps.

Run exactly one subscriber with `python mqtt_subscriber.py`. Its logs show broker
connection/subscription, rejected schemas, OTA transitions and DONE processing.
If OTA remains pending, verify MQTT ACLs and device credentials. If manifest or
download fails, verify DNS, public TLS certificate, ESP32 clock, `trust_base`,
capability expiry, file size/SHA, and reverse-proxy access-log policy.
