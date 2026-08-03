[![MseeP.ai Security Assessment Badge](https://mseep.net/pr/nullkraft-bk390a-multimeter-mcp-badge.png)](https://mseep.ai/app/nullkraft-bk390a-multimeter-mcp)

# BK390A-MultiMeter-MCP

This repo contains the BK Precision 390A parser and MCP server used during bring-up and meter-based verification.

The meter is an output-only instrument. It does not accept setup commands over the serial link. The MCP server reads the serial stream, decodes the measurement frame, and caches the most recent stable snapshot so later calls can reuse known state.

## Serial / Reachability Notes

- Serial format: `2400` baud, `7 data bits`, `odd parity`, `1 stop bit`
- If reads time out, check that the meter is powered, the cable is connected, and the front-panel `RS232` button has been pressed
- The timeout path now includes that RS232 hint
- In the last working session, the meter was reachable on `/dev/ttyUSB1`

## MCP Tools

- `bk390a_list_ports()`
- `bk390a_read(port="/dev/ttyUSB1", timeout_s=2.0, require_stable=True, max_frames=6)`
- `bk390a_read_raw_frame(port="/dev/ttyUSB1", timeout_s=2.0)`
- `bk390a_snapshot_get(port="/dev/ttyUSB1", timeout_s=2.0, require_stable=True, max_frames=6)`
- `bk390a_snapshot_cached(port="/dev/ttyUSB1")`
- `bk390a_snapshot_refresh(port="/dev/ttyUSB1", timeout_s=2.0, require_stable=True, max_frames=6)`
- `bk390a_apply_profile(profile, port="/dev/ttyUSB1", refresh_after=False, timeout_s=2.0)`

## Snapshot / Cache Behavior

- `bk390a_read()` returns a live decoded frame
- `bk390a_snapshot_refresh()` reads the meter, derives the current setup, and stores it in the cache
- `bk390a_snapshot_get()` returns the cached snapshot when available
- `bk390a_snapshot_cached()` never touches the serial port
- `bk390a_apply_profile()` records the expected front-panel setup for later comparison; it does not command the meter

The cached snapshot includes both the most recent decoded measurement and the derived setup fields such as function, mode, range, and status bits.

## What The Parser Decodes

The meter frame decoder maps the raw 9-character payload into:

- measurement function
- operating mode
- range code and range label
- display value and unit
- status bits
- option bits

That makes it useful for bring-up tasks where the lead position or front-panel state changes between readings.

## Example Workflow

1. List likely ports with `bk390a_list_ports()`
2. If the meter does not answer, press the front-panel `RS232` button
3. Read a live value with `bk390a_read()`
4. Refresh the snapshot cache with `bk390a_snapshot_refresh()`
5. Reuse the cached state with `bk390a_snapshot_get()` or `bk390a_snapshot_cached()`

## Phrases That Fit The Workflow

- “List BK meter ports.”
- “Read the BK meter live.”
- “Take a BK meter snapshot.”
- “Use the cached BK meter setup.”
- “Refresh the BK meter setup from the instrument.”
- “Record the BK meter expected setup as voltage DC auto range.”
- “Tell me whether the BK meter is reachable.”
