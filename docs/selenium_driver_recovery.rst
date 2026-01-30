Selenium Driver Freeze Detection and Recovery
=============================================

Overview
--------
When controlling a real browser with Selenium, the browser process may freeze, crash, or be closed
manually by the user. To make the system robust, `selenium_llm_base` now implements driver
responsiveness detection and automatic recovery:

- Quick health checks (window handles + current_url) with a configurable timeout.
- If the driver does not respond or raises errors, the system tries to quit the stale driver,
  clean up Chromium remnants, recreate a fresh shared driver, and retry the workflow.

Configuration
-------------
The following runtime-exposed configuration variables control this behavior:

- ``SELENIUM_DRIVER_RESPONSIVE_TIMEOUT`` (default 10s): How many seconds to wait for a trivial
  driver operation (e.g., accessing ``window_handles``) before considering the driver frozen.

- ``SELENIUM_DRIVER_RECOVERY_RETRIES`` (default 2): How many times to attempt restarting the
  browser and retrying the LLM workflow when the driver is detected to be frozen.

Behavior in the code
--------------------
- ``wait_until_response_stabilizes`` will detect repeated extraction errors and attempt a
  synchronous recovery of the driver (quitting, cleanup, recreate) and will raise a
  ``FrozenDriverError`` to indicate the caller that the workflow should be retried.

- ``generate_response`` (and the double-prompt flow) now run the complete workflow inside a
  driver-recovery retry loop: on driver-related failures the code will attempt to recover the
  driver and retry the workflow up to ``SELENIUM_DRIVER_RECOVERY_RETRIES`` times.

Notes
-----
- Recovery is implemented to be conservative: it prefers a graceful quit and recreation, with
  additional aggressive cleanup when needed. This should make the system resilient when the
  browser is unresponsive or closed by the user.

